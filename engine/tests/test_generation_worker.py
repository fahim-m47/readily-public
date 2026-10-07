"""Generation is serial, cached, cancellable, and routed by model."""

import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest
from conftest import make_entry, wait_until
from generation_fakes import entry_for
from storage_fakes import (
    AdvancingClock,
    create_plan,
    open_test_storage,
    publish_segment,
    strip_generation_records,
)
from worker_fakes import (
    RecordingPlayback,
    RecordingSynthesizer,
    request,
    voice_model,
    worker_for,
)

from readily_engine.catalog import load_manifest
from readily_engine.chunking import Block, Boundary, ChunkedSource
from readily_engine.generation import GeneratedAudio, GenerationRecord
from readily_engine.narration.timeline import timeline_starts
from readily_engine.narration.worker import (
    GenerationWorker,
    NarrationRequest,
)
from readily_engine.storage.history import NarrationStatus, SynthesisSettings


@pytest.mark.parametrize(
    "old_version,cached_version", [(1, 1), (1, 2), (1, 3), (2, 2), (3, 3)]
)
@pytest.mark.parametrize("shared_current_audio", [False, True])
def test_saved_qwen_narration_resynthesizes_after_reference_pinning(
    tmp_path, old_version, cached_version, shared_current_audio
):
    entry = load_manifest().find("qwen3-tts:0.6b")
    assert entry is not None
    storage = open_test_storage(tmp_path)
    old_synth = RecordingSynthesizer()
    worker = GenerationWorker(
        {
            entry.id: voice_model(
                old_synth, entry.model_copy(update={"version": old_version})
            )
        },
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        narration_id = worker.start(
            NarrationRequest(
                model=entry.id,
                voice=entry.default_voice,
                input="Saved Qwen.",
            )
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
    finally:
        worker.close()

    plan = storage.plan(narration_id)
    segment = storage.rekey_segment(
        narration_id,
        plan.segments[0],
        replace(plan.segments[0].generation, catalog_version=cached_version),
    )
    publish_segment(storage, plan, segment, np.ones(240, dtype=np.float32), 24_000)
    strip_generation_records(tmp_path / "readily.db")
    storage.close()

    synth = RecordingSynthesizer()
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry)},
        RecordingPlayback(),
        open_test_storage(tmp_path),
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        if shared_current_audio:
            worker.start(
                NarrationRequest(
                    model=entry.id,
                    voice=entry.default_voice,
                    input="Saved Qwen.",
                )
            )
            wait_until(lambda: worker.snapshot()["phase"] == "finished")
        worker.resume(narration_id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synth.inputs == ["Saved Qwen."]
        worker.resume(narration_id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synth.inputs == ["Saved Qwen."]
    finally:
        worker.close()


@pytest.mark.parametrize("first_regenerated", [False, True])
def test_qwen_resume_rebuilds_timing_before_skipping_retired_audio(
    tmp_path, first_regenerated
):
    entry = load_manifest().find("qwen3-tts:0.6b")
    storage = open_test_storage(tmp_path)
    settings = SynthesisSettings(
        entry.id, 1, entry.default_voice, 1.0, entry.tunables.pause_policy
    )
    plan = storage.create(
        ChunkedSource(
            "First. Second.",
            (
                Block("First.", 0, 6, Boundary.SENTENCE),
                Block("Second.", 7, 14, Boundary.PARAGRAPH),
            ),
        ),
        settings,
        entry_for(settings),
    )
    for segment in plan.segments:
        publish_segment(storage, plan, segment, np.ones(240, dtype=np.float32), 24_000)
    strip_generation_records(tmp_path / "readily.db")
    plan = storage.plan(plan.id)
    for segment in plan.segments:
        if first_regenerated and segment.ordinal == 0:
            segment = storage.rekey_segment(
                plan.id,
                segment,
                GenerationRecord.for_entry(entry, settings.voice_id, segment.text),
            )
            publish_segment(
                storage, plan, segment, np.ones(240, dtype=np.float32), 24_000
            )
    storage.set_status(plan.id, NarrationStatus.STOPPED, playhead_sec=11.0)
    synth = RecordingSynthesizer()
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        worker.resume(plan.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synth.inputs == (
            ["Second."] if first_regenerated else ["First.", "Second."]
        )
        assert timeline_starts(storage.plan(plan.id))[1] < 1.0
    finally:
        worker.close()
        storage.close()


def test_a_later_qwen_version_replays_audio_already_refreshed_under_a_verified_one(
    tmp_path,
):
    entry = load_manifest().find("qwen3-tts:0.6b")
    storage = open_test_storage(tmp_path)
    settings = SynthesisSettings(
        entry.id, 1, entry.default_voice, 1.0, entry.tunables.pause_policy
    )
    plan = storage.create(
        ChunkedSource(
            "Saved Qwen.", (Block("Saved Qwen.", 0, 11, Boundary.PARAGRAPH),)
        ),
        settings,
        entry_for(settings),
    )
    refreshed = storage.rekey_segment(
        plan.id,
        plan.segments[0],
        GenerationRecord.for_entry(entry, settings.voice_id, plan.segments[0].text),
    )
    publish_segment(storage, plan, refreshed, np.ones(240, dtype=np.float32), 24_000)
    storage.set_status(plan.id, NarrationStatus.STOPPED, playhead_sec=0.0)
    synth = RecordingSynthesizer()
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry.model_copy(update={"version": 5}))},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        worker.resume(plan.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synth.inputs == []
    finally:
        worker.close()
        storage.close()


def test_cached_audio_survives_an_uninstalled_voice_model(tmp_path):
    entry = load_manifest().find("qwen3-tts:0.6b")
    storage = open_test_storage(tmp_path)
    settings = SynthesisSettings(
        entry.id, 1, entry.default_voice, 1.0, entry.tunables.pause_policy
    )
    plan = storage.create(
        ChunkedSource(
            "Saved Qwen.", (Block("Saved Qwen.", 0, 11, Boundary.PARAGRAPH),)
        ),
        settings,
        entry_for(settings),
    )
    publish_segment(
        storage, plan, plan.segments[0], np.ones(240, dtype=np.float32), 24_000
    )
    worker = GenerationWorker(
        {},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        assert worker.cached(plan, plan.segments[0]) is not None
    finally:
        worker.close()
        storage.close()


def test_a_qwen_model_below_the_pinning_version_replays_older_audio(tmp_path):
    entry = load_manifest().find("qwen3-tts:0.6b")
    storage = open_test_storage(tmp_path)
    settings = SynthesisSettings(
        entry.id, 1, entry.default_voice, 1.0, entry.tunables.pause_policy
    )
    plan = storage.create(
        ChunkedSource(
            "Saved Qwen.", (Block("Saved Qwen.", 0, 11, Boundary.PARAGRAPH),)
        ),
        settings,
        entry_for(settings),
    )
    publish_segment(
        storage, plan, plan.segments[0], np.ones(240, dtype=np.float32), 24_000
    )
    storage.set_status(plan.id, NarrationStatus.STOPPED, playhead_sec=0.0)
    synth = RecordingSynthesizer()
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry.model_copy(update={"version": 3}))},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        worker.resume(plan.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synth.inputs == []
    finally:
        worker.close()
        storage.close()


def test_unspeakable_source_is_rejected_before_history_is_created(tmp_path):
    storage = open_test_storage(tmp_path)
    worker = worker_for(RecordingSynthesizer(), RecordingPlayback(), storage)
    try:
        with pytest.raises(ValueError, match="speakable text"):
            worker.start(request("\u200b \ufeff\u2060"))

        assert storage.history() == ()
        assert worker.snapshot()["phase"] == "idle"
    finally:
        worker.close()


def test_cache_miss_is_published_then_played_from_the_store(tmp_path):
    synth = RecordingSynthesizer()
    storage = open_test_storage(tmp_path)
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(request("Stored."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")

        assert synth.inputs == ["Stored."]
        detail = storage.history_detail(narration_id)
        assert detail.segments[0].audio_present is True
        assert detail.segments[0].duration_sec == pytest.approx(0.01)
        assert len(playback.played) == 1
    finally:
        worker.close()


def test_identical_narrations_share_the_cached_segment(tmp_path):
    synth = RecordingSynthesizer()
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        worker.start(request("Shared."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        second = worker.start(request("Shared."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")

        assert synth.inputs == ["Shared."]
        assert len(list((tmp_path / "segments").rglob("*.npz"))) == 1
        assert storage.history_detail(second).segments[0].duration_sec == pytest.approx(
            0.01
        )
    finally:
        worker.close()


def test_every_narration_uses_the_same_persistent_serial_thread(tmp_path):
    synth = RecordingSynthesizer()
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, open_test_storage(tmp_path))
    try:
        worker.start(request("first"))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        worker.start(request("second"))
        wait_until(
            lambda: (
                worker.snapshot()["phase"] == "finished"
                and synth.inputs == ["first", "second"]
            )
        )

        assert len(set(synth.thread_ids)) == 1
        assert synth.max_active == 1
        assert [sample_rate for _, sample_rate in playback.played] == [24_000, 24_000]
        assert worker.thread.is_alive()
    finally:
        worker.close()


def test_a_narration_is_synthesized_as_budgeted_blocks(tmp_path):
    synth = RecordingSynthesizer()
    worker = worker_for(synth, RecordingPlayback(), open_test_storage(tmp_path))
    try:
        worker.start(request("First. " + "x" * 451))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")

        assert synth.inputs == ["First.", "x" * 449, "xx"]
    finally:
        worker.close()


def test_starting_a_new_narration_invalidates_the_in_flight_result(tmp_path):
    release = threading.Event()
    synth = RecordingSynthesizer(hold=release)
    playback = RecordingPlayback()
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, playback, storage)
    try:
        first_id = worker.start(request("first"))
        wait_until(lambda: synth.inputs == ["first"])
        second_id = worker.start(request("second"))
        release.set()
        wait_until(
            lambda: (
                worker.snapshot()["phase"] == "finished"
                and synth.inputs == ["first", "second"]
            )
        )

        assert first_id != second_id
        assert len(playback.played) == 1
        assert worker.snapshot()["narrationId"] == second_id
        assert storage.history_detail(first_id).status is NarrationStatus.STOPPED
        assert storage.history_detail(second_id).status is NarrationStatus.FINISHED
    finally:
        worker.close()


def test_the_idle_snapshot_names_the_catalog_default_it_was_given(tmp_path):
    # Not a literal: the wire's idle snapshot has to name the entry the
    # server would resolve for a request that picks no model, and only the
    # Catalog knows which that is.
    worker = GenerationWorker(
        {"qwen3-tts:0.6b": voice_model(RecordingSynthesizer(), make_entry())},
        RecordingPlayback(),
        open_test_storage(tmp_path),
        default_model="qwen3-tts:0.6b",
        default_voice="Chelsie",
    )
    try:
        snapshot = worker.snapshot()

        assert snapshot["phase"] == "idle"
        assert snapshot["modelId"] == "qwen3-tts:0.6b"
        assert snapshot["voiceId"] == "Chelsie"
    finally:
        worker.close()


def test_prewarm_warms_the_catalog_default_not_a_hardcoded_model(tmp_path):
    class Countingynthesizer(RecordingSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.prewarms = 0

        def prewarm(self) -> bool:
            self.prewarms += 1
            return True

    instant, expressive = Countingynthesizer(), Countingynthesizer()
    worker = GenerationWorker(
        {
            "acme:1m": voice_model(instant, make_entry(), instant.prewarm),
            "qwen3-tts:0.6b": voice_model(
                expressive, load_manifest().find("qwen3-tts:0.6b"), expressive.prewarm
            ),
        },
        RecordingPlayback(),
        open_test_storage(tmp_path),
        default_model="qwen3-tts:0.6b",
        default_voice="Chelsie",
    )
    try:
        worker.prewarm()
        wait_until(lambda: expressive.prewarms == 1)

        assert instant.prewarms == 0
    finally:
        worker.close()


def test_prewarm_runs_on_the_worker_thread_and_leaves_state_idle(tmp_path):
    class PrewarmingSynthesizer(RecordingSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.prewarm_threads: list[int] = []

        def prewarm(self) -> bool:
            self.prewarm_threads.append(threading.get_ident())
            return True

    synthesizer = PrewarmingSynthesizer()
    worker = worker_for(
        synthesizer,
        RecordingPlayback(),
        open_test_storage(tmp_path),
        prewarm=synthesizer.prewarm,
    )
    try:
        worker.prewarm()
        wait_until(lambda: synthesizer.prewarm_threads)

        assert synthesizer.prewarm_threads == [worker.thread.ident]
        assert worker.snapshot()["phase"] == "idle"

        worker.start(request("After prewarm"))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synthesizer.thread_ids == [worker.thread.ident]
    finally:
        worker.close()


def test_prewarm_failure_leaves_the_worker_serving_narrations(tmp_path):
    class ExplodingPrewarmSynthesizer(RecordingSynthesizer):
        def prewarm(self) -> bool:
            raise RuntimeError("boom")

    synthesizer = ExplodingPrewarmSynthesizer()
    worker = worker_for(
        synthesizer,
        RecordingPlayback(),
        open_test_storage(tmp_path),
        prewarm=synthesizer.prewarm,
    )
    try:
        worker.prewarm()
        worker.start(request("Still alive"))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synthesizer.inputs == ["Still alive"]
    finally:
        worker.close()


def test_a_stored_block_reaches_playback_while_the_next_is_still_synthesizing(tmp_path):
    release = threading.Event()
    seen: list[str] = []

    class TwoBlockSynthesizer:
        def generate(self, record):
            text = record.text
            seen.append(text)
            if len(seen) > 1:
                release.wait(timeout=2)
            return GeneratedAudio(
                np.ones(240 if len(seen) == 1 else 480, dtype=np.float32), 24_000
            )

    playback = RecordingPlayback()
    worker = worker_for(TwoBlockSynthesizer(), playback, open_test_storage(tmp_path))
    try:
        worker.start(request("First. Second."))
        wait_until(lambda: len(playback.played) == 1)

        assert [len(pcm) for pcm, _ in playback.played] == [240]
        assert worker.snapshot()["phase"] == "playing"
        assert playback.drains == 0

        release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        # The second piece opens on the sentence seam's authored pause
        # (80ms at 24kHz), then carries the second Block's audio.
        assert [len(pcm) for pcm, _ in playback.played] == [240, 1920 + 480]
        assert playback.drains == 1
        assert worker.snapshot()["totalSec"] == pytest.approx(0.11)
    finally:
        worker.close()


def test_cancellation_discards_an_in_flight_block(tmp_path):
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class HoldingSynthesizer:
        def generate(self, record):
            try:
                started.set()
                release.wait(timeout=2)
                return GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)
            finally:
                closed.set()

    worker = worker_for(
        HoldingSynthesizer(), RecordingPlayback(), open_test_storage(tmp_path)
    )
    try:
        worker.start(request("cancel me"))
        wait_until(started.is_set)
        worker.stop()
        release.set()
        wait_until(closed.is_set)
        assert worker.snapshot()["phase"] == "idle"
    finally:
        worker.close()


def test_requests_route_to_the_model_they_name(tmp_path):
    class NamedSynthesizer:
        def __init__(self, name):
            self.name = name
            self.inputs = []

        def generate(self, record):
            text = record.text
            self.inputs.append(text)
            return GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)

    instant = NamedSynthesizer("acme:1m")
    expressive = NamedSynthesizer("qwen3-tts:0.6b")
    worker = GenerationWorker(
        {
            "acme:1m": voice_model(instant, make_entry()),
            "qwen3-tts:0.6b": voice_model(
                expressive, load_manifest().find("qwen3-tts:0.6b")
            ),
        },
        RecordingPlayback(),
        open_test_storage(tmp_path),
        default_model="acme:1m",
        default_voice="narrator",
    )
    try:
        worker.start(
            NarrationRequest(model="qwen3-tts:0.6b", input="hi", voice="Chelsie")
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert expressive.inputs == ["hi"]
        assert instant.inputs == []
        assert worker.snapshot()["modelId"] == "qwen3-tts:0.6b"
    finally:
        worker.close()


def test_a_model_without_a_synthesizer_fails_the_narration_cleanly(tmp_path):
    worker = worker_for(
        RecordingSynthesizer(), RecordingPlayback(), open_test_storage(tmp_path)
    )
    try:
        worker.start(NarrationRequest(model="missing:1b", input="hi", voice="v"))
        wait_until(lambda: worker.snapshot()["phase"] == "failed")
        assert worker.snapshot()["error"]["code"] == "generation_failed"
    finally:
        worker.close()


def test_reopened_storage_resumes_cached_audio_without_synthesis(tmp_path):
    first_storage = open_test_storage(tmp_path)
    narration = create_plan(first_storage, "Cached.")
    publish_segment(
        first_storage,
        narration,
        narration.segments[0],
        np.ones(240, dtype=np.float32),
        24_000,
    )
    first_storage.checkpoint(narration.id, 0.005)
    first_storage.set_status(narration.id, NarrationStatus.PLAYING)
    first_storage.close()

    reopened = open_test_storage(tmp_path)
    synth = RecordingSynthesizer()
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, reopened)
    try:
        assert worker.snapshot()["phase"] == "idle"
        worker.resume(narration.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")

        assert synth.inputs == []
        assert len(playback.played[0][0]) < 240
    finally:
        worker.close()


def test_replaying_age_evicted_audio_refreshes_recency_and_repopulates(tmp_path):
    clock = AdvancingClock(datetime(2026, 8, 1, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    narration = create_plan(storage, "Evicted.")
    publish_segment(
        storage,
        narration,
        narration.segments[0],
        np.ones(240, dtype=np.float32),
        24_000,
    )
    storage.set_status(
        narration.id,
        NarrationStatus.FINISHED,
        playhead_sec=0.01,
        total_duration_sec=0.01,
    )
    clock.advance(seconds=8 * 24 * 60 * 60)
    storage.update_retention(
        segment_budget_bytes=storage.retention().segment_budget_bytes,
        keep_audio_days=7,
    )
    assert storage.history_detail(narration.id).audio_present is False

    synth = RecordingSynthesizer()
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        worker.resume(narration.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")

        assert synth.inputs == ["Evicted."]
        detail = storage.history_detail(narration.id)
        assert detail.audio_present is True
        assert detail.last_played_at == clock.value
        assert storage.retention().audio_bytes > 0
    finally:
        worker.close()


def test_paused_full_ring_keeps_preparing_until_sixty_seconds_are_ready(tmp_path):
    from test_playback import Recorder

    release = threading.Event()

    class LongSynthesizer(RecordingSynthesizer):
        def generate(self, record):
            self.inputs.append(record.text)
            if len(self.inputs) > 1:
                release.wait(timeout=2)
            return GeneratedAudio(np.ones(20 * 24_000, dtype=np.float32), 24_000)

    synth = LongSynthesizer()
    playback = Recorder().player(clock=lambda: 0.0)
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(request("One.\n\nTwo.\n\nThree.\n\nFour."))
        wait_until(lambda: worker.snapshot()["phase"] == "playing")
        assert worker.pause()
        before = sum(
            s.audio_present for s in storage.history_detail(narration_id).segments
        )
        assert before == 1
        release.set()
        wait_until(
            lambda: (
                sum(
                    s.audio_present
                    for s in storage.history_detail(narration_id).segments
                )
                == 3
            )
        )
        time.sleep(0.1)
        assert synth.inputs == ["One.", "Two.", "Three."]
        assert playback.position(1) == 0.0
        assert worker.snapshot()["phase"] == "paused"
        assert worker.snapshot()["totalSec"] == pytest.approx(60.8)
    finally:
        release.set()
        assert worker.close()
        playback.close()


def test_a_paused_resume_holds_the_playhead_until_play(tmp_path):
    from test_playback import Recorder

    storage = open_test_storage(tmp_path)
    narration = create_plan(storage, "Opened, not heard.")
    storage.set_status(narration.id, NarrationStatus.STOPPED, playhead_sec=0.0)
    playback = Recorder().player(clock=lambda: 0.0)
    worker = worker_for(RecordingSynthesizer(), playback, storage)
    try:
        worker.resume(narration.id, paused=True)
        wait_until(lambda: worker.snapshot()["phase"] == "paused")
        time.sleep(0.05)
        assert worker.snapshot()["phase"] == "paused"
        assert worker.snapshot()["positionSec"] == 0.0
        assert storage.history_detail(narration.id).status == NarrationStatus.PLAYING

        assert worker.play()
        assert worker.snapshot()["phase"] == "playing"
    finally:
        worker.close()
        playback.close()
        storage.close()


def test_a_fill_synthesizes_and_stores_one_block_for_export(tmp_path):
    storage = open_test_storage(tmp_path)
    synth = RecordingSynthesizer()
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        plan = create_plan(storage, "Evicted.")

        audio = worker.fill(plan, plan.segments[0])

        assert audio is not None
        assert synth.inputs == ["Evicted."]
        assert storage.raw_audio(plan.segments[0]) is not None
    finally:
        worker.close()


def test_a_fill_never_preempts_the_narration_being_heard(tmp_path):
    # ADR 0002 §3: one synthesizer, one process-long thread. An Export that
    # needs synthesis has to queue behind what is playing rather than
    # interrupt it, so the button press is instant and the listener's audio
    # is untouched.
    release = threading.Event()
    storage = open_test_storage(tmp_path)
    synth = RecordingSynthesizer(hold=release)
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, storage)
    try:
        worker.start(request("Playing now."))
        wait_until(lambda: synth.inputs == ["Playing now."])
        plan = create_plan(storage, "Exported later.")
        filled: list[object] = []
        export = threading.Thread(
            target=lambda: filled.append(worker.fill(plan, plan.segments[0]))
        )
        export.start()
        try:
            # The Narration is still mid-synthesis and the Fill is queued
            # behind it: nothing about the playing Narration has changed.
            time.sleep(0.05)
            assert synth.inputs == ["Playing now."]
            assert worker.snapshot()["narrationId"] != plan.id
        finally:
            release.set()
        export.join(timeout=2)

        assert filled[0] is not None
        assert synth.inputs == ["Playing now.", "Exported later."]
        assert synth.max_active == 1
    finally:
        worker.close()


def test_a_fill_queued_past_shutdown_releases_its_waiter(tmp_path):
    # Everything behind the shutdown sentinel is dead work. An Export
    # blocked on a Fill the worker will never reach has to be told so
    # rather than parking a thread for the rest of the process.
    storage = open_test_storage(tmp_path)
    worker = worker_for(RecordingSynthesizer(), RecordingPlayback(), storage)
    plan = create_plan(storage, "Never filled.")
    worker.close()

    assert worker.fill(plan, plan.segments[0]) is None


def test_a_fill_of_a_deleted_narration_answers_rather_than_hanging(tmp_path):
    storage = open_test_storage(tmp_path)
    worker = worker_for(RecordingSynthesizer(), RecordingPlayback(), storage)
    try:
        plan = create_plan(storage, "Deleted.")
        storage.delete(plan.id)

        assert worker.fill(plan, plan.segments[0]) is None
    finally:
        worker.close()


def test_retention_keeps_prepared_audio_until_playback_releases_it(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class HeldPlayback(RecordingPlayback):
        def feed(self, pcm, sample_rate, generation, *, pause_frames=0):
            entered.set()
            self.wait(generation, release.is_set)
            return super().feed(pcm, sample_rate, generation)

    storage = open_test_storage(tmp_path)
    worker = worker_for(RecordingSynthesizer(), HeldPlayback(), storage)
    try:
        narration_id = worker.start(request("First.\n\nSecond."))
        assert entered.wait(timeout=2)
        wait_until(lambda: storage.history_detail(narration_id).audio_present)
        storage.update_retention(segment_budget_bytes=1, keep_audio_days=None)
        assert storage.history_detail(narration_id).audio_present
        release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
    finally:
        release.set()
        worker.close()
    storage.run_retention()
    assert not storage.history_detail(narration_id).audio_present


def test_a_resume_onto_an_entry_without_the_voice_names_the_missing_voice(
    tmp_path, caplog
):
    storage = open_test_storage(tmp_path)
    narration = create_plan(storage, "Retired voice.")
    storage.set_status(narration.id, NarrationStatus.STOPPED)
    retired = make_entry(
        version=2,
        voices=[{"id": "af_other", "name": "Other", "language": "en-US"}],
        default_voice="af_other",
    )
    worker = GenerationWorker(
        {"acme:1m": voice_model(RecordingSynthesizer(), retired)},
        RecordingPlayback(),
        storage,
        default_model="acme:1m",
        default_voice="af_other",
    )
    try:
        with caplog.at_level(logging.ERROR):
            worker.resume(narration.id)
            wait_until(lambda: worker.snapshot()["phase"] == "failed")
        assert "offers no voice 'narrator'" in caplog.text
    finally:
        worker.close()
