"""Cancelling a Narration stops the model's work, not just the listening."""

import threading
import time
from datetime import UTC, datetime

import numpy as np
from conftest import make_entry, wait_until
from storage_fakes import AdvancingClock, open_test_storage
from worker_fakes import (
    RecordingPlayback,
    RecordingSynthesizer,
    request,
    voice_model,
    worker_for,
)

from readily_engine.catalog import load_manifest
from readily_engine.generation import GeneratedAudio
from readily_engine.narration.worker import GenerationWorker, NarrationRequest
from readily_engine.storage.history import NarrationStatus

# Long enough that a test waiting it out would time out: the only way the
# synthesizer exits in time is by being interrupted.
SPIN_SECONDS = 10.0


def _step(total: int) -> int:
    return total + 1


class SpinningSynthesizer:
    """Spins in Python for `SPIN_SECONDS` on any Block but those in `fast`,
    calling a Python function every step the way a model's token loop does.
    Records whether each Block ran to completion or was cut short."""

    def __init__(self, fast: frozenset[str] = frozenset()) -> None:
        self.fast = fast
        self.spinning = threading.Event()
        self.exited = threading.Event()
        self.completed: list[str] = []

    def generate(self, record):
        audio = GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)
        if record.text in self.fast:
            return audio
        self.spinning.set()
        try:
            total = 0
            deadline = time.monotonic() + SPIN_SECONDS
            while time.monotonic() < deadline:
                total = _step(total)
            self.completed.append(record.text)
            return audio
        finally:
            self.exited.set()


class SpinningOnRepeatSynthesizer(SpinningSynthesizer):
    """Fast the first time it sees a Block's text, so a Narration can finish;
    spins when the same text comes back, as a reroll or a resume after
    eviction brings it."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: set[str] = set()

    def generate(self, record):
        if record.text in self.seen:
            return super().generate(record)
        self.seen.add(record.text)
        return GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)


def test_stopping_mid_synthesis_interrupts_it_and_discards_an_empty_narration(
    tmp_path,
):
    synth = SpinningSynthesizer()
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("Never finished."))
        wait_until(synth.spinning.is_set)

        assert worker.stop() is True

        wait_until(synth.exited.is_set, timeout=1.0)
        assert synth.completed == []
        assert storage.history_detail(narration_id) is None
        assert storage.retention().audio_bytes == 0
    finally:
        worker.close()


def test_stopping_after_a_finished_block_keeps_it_and_saves_the_narration_stopped(
    tmp_path,
):
    synth = SpinningSynthesizer(fast=frozenset({"First."}))
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("First. Second."))
        wait_until(synth.spinning.is_set)

        assert worker.stop() is True

        wait_until(synth.exited.is_set, timeout=1.0)
        assert synth.completed == []
        assert storage.history_detail(narration_id).status is NarrationStatus.STOPPED
        first, second = storage.plan(narration_id).segments
        assert storage.has_verified_audio(first)
        assert not storage.has_verified_audio(second)
    finally:
        worker.close()


def test_stopping_a_reroll_keeps_the_narration_and_its_original_take(tmp_path):
    synth = SpinningOnRepeatSynthesizer()
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("One Block."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        original = storage.plan(narration_id).segments[0]
        audio_bytes = storage.retention().audio_bytes
        assert audio_bytes > 0

        assert worker.select_take(narration_id, 0, "reroll")
        wait_until(synth.spinning.is_set)
        assert worker.stop() is True

        wait_until(synth.exited.is_set, timeout=1.0)
        assert synth.completed == []
        assert storage.history_detail(narration_id).status is NarrationStatus.STOPPED
        assert storage.has_verified_audio(original)
        assert storage.retention().audio_bytes == audio_bytes
    finally:
        worker.close()


def test_stopping_a_resume_after_eviction_keeps_the_narration(tmp_path):
    clock = AdvancingClock(datetime(2026, 8, 1, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    synth = SpinningOnRepeatSynthesizer()
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("Evicted."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        clock.advance(seconds=8 * 24 * 60 * 60)
        storage.update_retention(
            segment_budget_bytes=storage.retention().segment_budget_bytes,
            keep_audio_days=7,
        )
        assert storage.history_detail(narration_id).audio_present is False

        worker.resume(narration_id)
        wait_until(synth.spinning.is_set)
        assert worker.stop() is True

        wait_until(synth.exited.is_set, timeout=1.0)
        assert synth.completed == []
        assert storage.history_detail(narration_id).status is NarrationStatus.STOPPED
    finally:
        worker.close()


def test_the_worker_serves_the_next_narration_after_an_interrupted_one(tmp_path):
    synth = SpinningSynthesizer(fast=frozenset({"Next."}))
    worker = worker_for(synth, RecordingPlayback(), open_test_storage(tmp_path))
    try:
        worker.start(request("Interrupted."))
        wait_until(synth.spinning.is_set)
        thread = worker.thread

        worker.start(request("Next."))

        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synth.completed == []
        assert worker.thread is thread and thread.is_alive()
        assert threading.gettrace() is None
    finally:
        worker.close()


def test_a_narration_on_another_model_releases_the_one_loaded_before_it(tmp_path):
    events: list[str] = []

    class LoggingSynthesizer(RecordingSynthesizer):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

        def generate(self, record):
            events.append(f"generate {self.name}")
            return super().generate(record)

    def unloading(name: str):
        return lambda: events.append(f"unload {name}")

    worker = GenerationWorker(
        {
            "acme:1m": voice_model(
                LoggingSynthesizer("acme"), make_entry(), unload=unloading("acme")
            ),
            "qwen3-tts:0.6b": voice_model(
                LoggingSynthesizer("qwen"),
                load_manifest().find("qwen3-tts:0.6b"),
                unload=unloading("qwen"),
            ),
        },
        RecordingPlayback(),
        open_test_storage(tmp_path),
        default_model="acme:1m",
        default_voice="narrator",
    )
    try:
        for model, voice, text in [
            ("acme:1m", "narrator", "One."),
            ("acme:1m", "narrator", "Two."),
            ("qwen3-tts:0.6b", "Chelsie", "Three."),
        ]:
            worker.start(NarrationRequest(model=model, input=text, voice=voice))
            wait_until(lambda: worker.snapshot()["phase"] == "finished")

        assert events == [
            "generate acme",
            "generate acme",
            "unload acme",
            "generate qwen",
        ]
    finally:
        worker.close()


def test_starting_on_another_model_mid_synthesis_cuts_the_first_short_and_unloads_it(
    tmp_path,
):
    events: list[str] = []
    spinning = SpinningSynthesizer()

    class LoggingSynthesizer(RecordingSynthesizer):
        def generate(self, record):
            events.append("generate qwen")
            return super().generate(record)

    worker = GenerationWorker(
        {
            "acme:1m": voice_model(
                spinning, make_entry(), unload=lambda: events.append("unload acme")
            ),
            "qwen3-tts:0.6b": voice_model(
                LoggingSynthesizer(),
                load_manifest().find("qwen3-tts:0.6b"),
                unload=lambda: events.append("unload qwen"),
            ),
        },
        RecordingPlayback(),
        open_test_storage(tmp_path),
        default_model="acme:1m",
        default_voice="narrator",
    )
    try:
        worker.start(request("Long."))
        wait_until(spinning.spinning.is_set)

        worker.start(
            NarrationRequest(model="qwen3-tts:0.6b", input="Next.", voice="Chelsie")
        )

        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert spinning.completed == []
        assert events == ["unload acme", "generate qwen"]
    finally:
        worker.close()
