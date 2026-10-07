"""Pause, play, stop, and seek as the listener drives them."""

import asyncio
import threading
import time

import numpy as np
import pytest
from conftest import make_entry, wait_until
from generation_fakes import entry_for
from storage_fakes import SETTINGS, open_test_storage, publish_segment
from worker_fakes import (
    KOKORO,
    HoldSecondBlockSynthesizer,
    RecordingPlayback,
    RecordingStorage,
    RecordingSynthesizer,
    request,
    voice_model,
    worker_for,
)

from readily_engine.catalog import load_manifest
from readily_engine.chunking import Block, Boundary, ChunkedSource, chunk
from readily_engine.generation import GeneratedAudio, GenerationRecord
from readily_engine.narration.worker import (
    GenerationWorker,
)
from readily_engine.storage.history import NarrationStatus, SynthesisSettings


def test_seek_reaches_cached_blocks_the_device_has_never_played(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class HoldFirstFeed(RecordingPlayback):
        def feed(self, pcm, sample_rate, generation, *, pause_frames=0):
            if generation == 1:
                entered.set()
                release.wait(timeout=2)
            return super().feed(pcm, sample_rate, generation)

    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("One.\n\nTwo.\n\nThree.", KOKORO), SETTINGS, entry_for(SETTINGS)
    )
    for segment in plan.segments:
        publish_segment(storage, plan, segment, np.ones(240, dtype=np.float32), 24_000)
    storage.set_status(plan.id, NarrationStatus.STOPPED)
    synth = RecordingSynthesizer()
    playback = HoldFirstFeed()
    worker = worker_for(synth, playback, storage)
    try:
        worker.resume(plan.id)
        assert entered.wait(timeout=2)
        assert worker.seek(plan.segments[2].source_start) is True
        assert worker.snapshot()["positionSec"] == pytest.approx(0.82)
        release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synth.inputs == []
        assert len(playback.played) == 1
        assert len(playback.played[0][0]) == 240
    finally:
        release.set()
        worker.close()


def test_time_seek_trims_audio_within_a_cached_block(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class HoldFirstFeed(RecordingPlayback):
        def feed(self, pcm, sample_rate, generation, *, pause_frames=0):
            if generation == 1:
                entered.set()
                release.wait(timeout=2)
            return super().feed(pcm, sample_rate, generation)

    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("One.\n\nTwo.\n\nThree.", KOKORO), SETTINGS, entry_for(SETTINGS)
    )
    for segment in plan.segments:
        publish_segment(storage, plan, segment, np.ones(240, dtype=np.float32), 24_000)
    storage.set_status(plan.id, NarrationStatus.STOPPED)
    synth = RecordingSynthesizer()
    playback = HoldFirstFeed()
    worker = worker_for(synth, playback, storage)
    try:
        worker.resume(plan.id)
        assert entered.wait(timeout=2)
        assert worker.seek_time(0.825) is True
        assert worker.snapshot()["positionSec"] == pytest.approx(0.825)
        release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synth.inputs == []
        assert len(playback.played) == 1
        assert len(playback.played[0][0]) == 120
        assert playback.played[0][0][0] == 0
        assert np.max(playback.played[0][0]) > 0
        assert worker.seek_time(0.0) is False
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize("retired_ordinal", [1, 2])
def test_seek_waits_for_current_lengths_before_jumping_past_retired_audio(
    tmp_path, retired_ordinal
):
    release = threading.Event()

    entry = load_manifest().find("qwen3-tts:0.6b")
    settings = SynthesisSettings(
        entry.id, 1, entry.default_voice, 1.0, entry.tunables.pause_policy
    )
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        ChunkedSource(
            "First. Second. Third.",
            (
                Block("First.", 0, 6, Boundary.PARAGRAPH),
                Block("Second.", 7, 14, Boundary.PARAGRAPH),
                Block("Third.", 15, 21, Boundary.PARAGRAPH),
            ),
        ),
        settings,
        entry_for(settings),
    )
    for part in plan.segments:
        if part.ordinal != retired_ordinal:
            part = storage.rekey_segment(
                plan.id,
                part,
                GenerationRecord.for_entry(entry, settings.voice_id, part.text),
            )
        publish_segment(storage, plan, part, np.ones(240, dtype=np.float32), 24_000)
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(tmp_path / "readily.db")) as connection, connection:
        connection.execute(
            "UPDATE narration_segments SET generation_json = NULL WHERE ordinal = ?",
            (retired_ordinal,),
        )
    storage.set_status(plan.id, NarrationStatus.STOPPED)

    synth, playback = RecordingSynthesizer(hold=release), RecordingPlayback()
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry)},
        playback,
        RecordingStorage(storage),
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        worker.resume(plan.id)
        wait_until(lambda: synth.inputs == [plan.segments[retired_ordinal].text])
        before = worker.snapshot()
        if retired_ordinal == 1:
            assert worker.seek(plan.segments[2].source_start) is False
            assert worker.snapshot()["phase"] == before["phase"]
            assert worker.snapshot()["positionSec"] == before["positionSec"]
        else:
            assert worker.seek(plan.segments[2].source_start) is True
            assert worker.snapshot()["phase"] == "preparing"
    finally:
        release.set()
        worker.close()
        storage.close()


def test_replay_keeps_saved_pauses_after_the_catalog_changes(tmp_path):
    storage = open_test_storage(tmp_path)
    playback = RecordingPlayback()
    worker = worker_for(RecordingSynthesizer(), playback, storage)
    try:
        narration_id = worker.start(request("First. Second.\n\nThird."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        original = np.concatenate([pcm for pcm, _ in playback.played])
    finally:
        worker.close()

    updated = KOKORO.model_copy(
        update={
            "pause_sentence_ms": 500,
            "pause_paragraph_break_ms": 1000,
        }
    )
    playback = RecordingPlayback()
    worker = GenerationWorker(
        {
            "acme:1m": voice_model(
                RecordingSynthesizer(), make_entry(tunables=updated.model_dump())
            )
        },
        playback,
        storage,
        default_model="acme:1m",
        default_voice="narrator",
    )
    try:
        worker.resume(narration_id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        np.testing.assert_array_equal(
            np.concatenate([pcm for pcm, _ in playback.played]), original
        )
        assert storage.resume(narration_id).settings.pause_policy == KOKORO.pause_policy
    finally:
        worker.close()


def test_stop_cancels_in_flight_audio_and_returns_to_the_idle_phase(tmp_path):
    release = threading.Event()
    synth = RecordingSynthesizer(hold=release)
    playback = RecordingPlayback()
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(request("stop me"))
        wait_until(lambda: synth.inputs == ["stop me"])

        assert worker.stop() is True
        assert worker.stop() is False
        release.set()
        wait_until(lambda: synth.active == 0)

        snapshot = worker.snapshot()
        assert snapshot["phase"] == "idle"
        assert snapshot["narrationId"] is None
        assert playback.played == []
        assert playback.stop_calls >= 1
        # Stopped before a word was ready: nothing to resume, so no row.
        assert storage.history_detail(narration_id) is None
    finally:
        worker.close()


def test_pause_freezes_the_playing_phase_and_play_releases_it(tmp_path):
    release = threading.Event()
    synth = HoldSecondBlockSynthesizer(release)
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, open_test_storage(tmp_path))
    try:
        worker.start(request("First. Second."))
        wait_until(lambda: worker.snapshot()["phase"] == "playing")

        assert worker.pause() is True
        assert worker.snapshot()["phase"] == "paused"
        assert playback.paused is True
        assert worker.pause() is False

        assert worker.play() is True
        assert worker.snapshot()["phase"] == "playing"
        assert playback.paused is False
        assert worker.play() is False

        release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert worker.pause() is False
    finally:
        release.set()
        worker.close()


def test_events_mirror_the_live_playhead_while_playing(tmp_path):
    release = threading.Event()
    synth = HoldSecondBlockSynthesizer(release)

    class Positioned(RecordingPlayback):
        def position(self, generation: int) -> float:
            return 1.25 if generation == self.generation else 0.0

    worker = worker_for(synth, Positioned(), open_test_storage(tmp_path))
    try:
        worker.start(request("First. Second."))
        wait_until(lambda: worker.snapshot()["phase"] == "playing")

        async def first_snapshot():
            async for snapshot in worker.events():
                return snapshot

        snapshot = asyncio.run(first_snapshot())
        assert snapshot["phase"] == "playing"
        assert snapshot["positionSec"] == pytest.approx(1.25)
    finally:
        release.set()
        worker.close()


def test_the_live_playhead_never_reaches_the_stored_state(tmp_path):
    release = threading.Event()
    synth = HoldSecondBlockSynthesizer(release)

    class Positioned(RecordingPlayback):
        def position(self, generation: int) -> float:
            return 1.25 if generation == self.generation else 0.0

        def level(self, generation: int) -> float:
            return 0.6 if generation == self.generation else 0.0

    worker = worker_for(synth, Positioned(), open_test_storage(tmp_path))
    try:
        worker.start(request("First. Second."))
        wait_until(lambda: worker.snapshot()["phase"] == "playing")
        assert worker.snapshot()["positionSec"] == pytest.approx(1.25)
        assert worker.snapshot()["level"] == pytest.approx(0.6)
        assert worker._state.snapshot()["positionSec"] == 0.0
        assert worker._state.snapshot()["level"] == 0.0
    finally:
        release.set()
        worker.close()


def test_stop_while_paused_returns_to_idle_and_persists_stopped(tmp_path):
    release = threading.Event()
    synth = HoldSecondBlockSynthesizer(release)
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("First. Second."))
        wait_until(lambda: worker.snapshot()["phase"] == "playing")
        assert worker.pause() is True

        assert worker.stop() is True
        assert worker.snapshot()["phase"] == "idle"
        release.set()
        wait_until(
            lambda: (
                storage.history_detail(narration_id).status is NarrationStatus.STOPPED
            )
        )
    finally:
        release.set()
        worker.close()


def test_seeking_backward_then_forward_reprioritizes_without_resynthesis(tmp_path):
    release = threading.Event()
    synth = HoldSecondBlockSynthesizer(release)
    playback = RecordingPlayback()
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(request("First. Second."))
        wait_until(lambda: worker.snapshot()["phase"] == "playing")
        assembled = worker.snapshot()["totalSec"]
        assert assembled > 0

        assert worker.seek(0) is True
        assert worker.snapshot()["phase"] == "preparing"
        assert worker.seek(2) is True
        release.set()
        # The seek moves through audio that is still assembled, so the
        # length a client hears never drops while the walk catches up.
        seen: list[float] = []
        wait_until(
            lambda: (
                seen.append(worker.snapshot()["totalSec"])
                or worker.snapshot()["phase"] == "finished"
            )
        )
        assert min(seen) >= assembled

        # The first Block replayed from the Segment cache both times.
        assert synth.inputs.count("First.") == 1
        # An offset inside the first Block snaps to its beginning.
        replayed = playback.played[-2][0]
        assert len(replayed) == 240
        assert storage.history_detail(narration_id).status is NarrationStatus.FINISHED
        assert worker.seek(0) is False
    finally:
        release.set()
        worker.close()


def test_seeking_after_a_catalog_bump_replays_the_rekeyed_blocks(tmp_path):
    """The seek re-queues the active plan. If the Blocks this run rekeyed
    never reach that plan, the seek hands the worker the pre-bump keys and
    every Block it just stored is synthesized all over again."""
    release = threading.Event()
    synth = HoldSecondBlockSynthesizer(release)
    storage = open_test_storage(tmp_path)
    bumped = GenerationWorker(
        {
            "acme:1m": voice_model(
                synth,
                make_entry().model_copy(update={"version": 2}),
            )
        },
        RecordingPlayback(),
        storage,
        default_model="acme:1m",
        default_voice="narrator",
    )
    try:
        # Planned under Catalog version 1, played by an Engine whose Catalog
        # has moved to 2, so each Block is rekeyed as the worker reaches it.
        plan = storage.create(
            chunk("First. Second.", KOKORO), SETTINGS, entry_for(SETTINGS)
        )
        storage.set_status(plan.id, NarrationStatus.STOPPED)
        bumped.resume(plan.id)
        wait_until(lambda: bumped.snapshot()["phase"] == "playing")
        assert synth.inputs[0] == "First."

        assert bumped.seek(0) is True
        release.set()
        wait_until(lambda: bumped.snapshot()["phase"] == "finished")

        # The first Block replayed from the Segment stored under its bumped
        # key; only the second was ever synthesized twice.
        assert synth.inputs.count("First.") == 1
    finally:
        release.set()
        bumped.close()


def test_a_backward_seek_lowers_the_durable_playhead(tmp_path):
    """The whole point of a backward seek is the listener's new place. If
    the durable playhead only ever moves forward, stopping (or crashing)
    right after one resumes at the position they were trying to leave."""

    class PlayedOnce(RecordingPlayback):
        """Reports a position for the first Narration only, the way a real
        device reports nothing for a generation it has not been fed yet."""

        def position(self, generation: int) -> float:
            if generation != self.generation:
                return 0.0
            return 5.0 if self.generation <= 1 else 0.0

    storage = open_test_storage(tmp_path)
    worker = worker_for(
        RecordingSynthesizer(hold=threading.Event()),
        PlayedOnce(),
        storage,
        checkpoint_seconds=0.01,
    )
    try:
        narration_id = worker.start(request("Rewound."))
        wait_until(lambda: storage.history_detail(narration_id).playhead_sec == 5.0)

        assert worker.seek(2) is True

        wait_until(lambda: storage.history_detail(narration_id).playhead_sec == 0.0)
        time.sleep(0.05)  # and it stays there, checkpoint after checkpoint
        assert storage.history_detail(narration_id).playhead_sec == 0.0
    finally:
        worker.close()


def test_seek_prepares_a_target_whose_length_is_not_known_yet(tmp_path):
    release = threading.Event()
    synth = HoldSecondBlockSynthesizer(release)
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        worker.start(request("First.\n\nSecond."))
        wait_until(lambda: worker.snapshot()["phase"] == "playing")
        assert worker.seek(8) is True
        assert worker.snapshot()["phase"] == "preparing"
    finally:
        release.set()
        worker.close()


def test_a_seek_uses_lengths_published_during_this_run(tmp_path):
    """A seek reads current lengths, including audio generated since admission."""

    class HoldThirdBlockSynthesizer:
        def __init__(self, release: threading.Event) -> None:
            self.release = release
            self.inputs: list[str] = []

        def generate(self, record):
            text = record.text
            self.inputs.append(text)
            if len(self.inputs) > 2:
                self.release.wait(timeout=2)
            return GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)

    class CountingStorage(RecordingStorage):
        def __init__(self, wrapped) -> None:
            super().__init__(wrapped)
            self.reads: list[int] = []

        def raw_audio(self, segment):
            self.reads.append(self.ordinals[segment.key])
            return self.wrapped.raw_audio(segment)

    release = threading.Event()
    synth = HoldThirdBlockSynthesizer(release)
    storage = CountingStorage(open_test_storage(tmp_path))
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        worker.start(request("One.\n\nTwo.\n\nThree."))
        # Both completed Blocks now have trimmed lengths;
        # the third is parked in synthesis, so the seek keeps `preparing`.
        wait_until(lambda: synth.inputs == ["One.", "Two.", "Three."])
        storage.reads.clear()

        # A paragraph pause is 400ms, so the second Block opens at 0.41s.
        assert worker.seek(6) is True
        release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "finished")

        # The first Block ends before the target and is never touched again.
        assert 0 not in storage.reads
        assert 1 in storage.reads
    finally:
        release.set()
        worker.close()
