"""Preparation follows ready listening time, independently of the device ring."""

import threading
import time

import numpy as np
import pytest
from conftest import wait_until
from generation_fakes import entry_for
from storage_fakes import SETTINGS, open_test_storage, publish_segment
from test_playback import Recorder
from worker_fakes import (
    KOKORO,
    FixedSynthesizer,
    HeldPlayback,
    PositionedPlayback,
    RecordingPlayback,
    RecordingStorage,
    RecordingSynthesizer,
    request,
    worker_for,
)

from readily_engine.chunking import chunk
from readily_engine.narration.measure import recover_lengths, trimmed_audio
from readily_engine.narration.timeline import timeline_starts
from readily_engine.storage.history import NarrationStatus


def test_healthy_inflight_generation_does_not_warn_before_the_next_block(tmp_path):
    release = threading.Event()
    entered = threading.Event()
    elapsed = 0.0

    class SustainableSynthesizer(FixedSynthesizer):
        def generate(self, record):
            nonlocal elapsed
            if self.inputs:
                entered.set()
                release.wait(timeout=5)
            elapsed += 5
            return super().generate(record)

    playback = PositionedPlayback(0)
    worker = worker_for(
        SustainableSynthesizer(20),
        playback,
        open_test_storage(tmp_path),
        clock=lambda: elapsed,
    )
    try:
        worker.set_speed(4)
        worker.start(request("First paragraph.\n\nSecond paragraph."))
        assert entered.wait(timeout=2)
        wait_until(lambda: worker.snapshot()["phase"] == "playing")
        elapsed += 2
        playback.position_sec = 6
        assert worker.snapshot()["generationBehind"] is False
        release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert worker.snapshot()["generationBehind"] is False
    finally:
        release.set()
        worker.close()


def test_stalled_generation_warns_when_the_device_drains_with_a_held_stretch_tail(
    tmp_path,
):
    release = threading.Event()
    entered = threading.Event()
    elapsed = 0.0

    class SustainableSynthesizer(FixedSynthesizer):
        def generate(self, record):
            nonlocal elapsed
            if self.inputs:
                entered.set()
                release.wait(timeout=5)
            elapsed += 5
            return super().generate(record)

    recorder = Recorder()
    playback = recorder.player()
    worker = worker_for(
        SustainableSynthesizer(20, value=0.25),
        playback,
        open_test_storage(tmp_path),
        clock=lambda: elapsed,
    )
    try:
        worker.set_speed(4)
        worker.start(request("First paragraph.\n\nSecond paragraph."))
        assert entered.wait(timeout=2)
        wait_until(lambda: bool(recorder.streams))
        deadline = time.monotonic() + 2
        while worker.snapshot()["positionSec"] < 19.8:
            assert time.monotonic() < deadline
            recorder.streams[0].callback_once(4096)
            time.sleep(0.001)
        # The feeder may still be trickling the first Block's last piece
        # into the ring, and queued audio clears the starving signal, so
        # one drain is not enough: keep draining until the device is dry.
        while not worker.snapshot()["generationBehind"]:
            assert time.monotonic() < deadline
            recorder.streams[0].callback_once(48000)
            time.sleep(0.001)
        snapshot = worker.snapshot()
        assert 19.8 < snapshot["positionSec"] < snapshot["totalSec"]
        worker.pause()
        assert worker.snapshot()["generationBehind"] is False
        worker.stop()
        assert worker.snapshot()["generationBehind"] is False
    finally:
        release.set()
        worker.close()
        playback.close()


def test_slow_generation_warns_during_first_block_and_reacts_to_live_speed(tmp_path):
    release = threading.Event()
    elapsed = 0.0

    class SlowSynthesizer(FixedSynthesizer):
        def generate(self, record):
            nonlocal elapsed
            if self.inputs:
                release.wait(timeout=5)
            elapsed += 8
            return super().generate(record)

    playback = PositionedPlayback(0)
    worker = worker_for(
        SlowSynthesizer(20),
        playback,
        open_test_storage(tmp_path),
        clock=lambda: elapsed,
    )
    try:
        worker.set_speed(4)
        worker.start(request("First paragraph.\n\nSecond paragraph."))
        wait_until(lambda: worker.snapshot()["phase"] == "playing")
        assert worker.snapshot()["generationBehind"] is True
        assert worker.snapshot()["speed"] == 4
        worker.set_speed(1)
        assert worker.snapshot()["generationBehind"] is False
        worker.set_speed(4)
        assert worker.snapshot()["generationBehind"] is True
        worker.pause()
        assert worker.snapshot()["generationBehind"] is False
        worker.play()
        release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert worker.snapshot()["generationBehind"] is False
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize("speed", [1.0, 4.0])
def test_prepare_first_finishes_synthesis_before_feeding_audio(tmp_path, speed):
    playback = RecordingPlayback()

    class ObservePlayback(FixedSynthesizer):
        def generate(self, record):
            assert playback.played == []
            return super().generate(record)

    synth = ObservePlayback(20.0)
    storage = open_test_storage(tmp_path)
    entry = entry_for(SETTINGS)
    storage.set_control_overrides(entry, entry.default_voice, {"prepare_first": True})
    worker = worker_for(synth, playback, storage, streaming=False)
    try:
        worker.set_speed(speed)
        narration_id = worker.start(
            request("\n\n".join(f"Paragraph {i}." for i in range(5)))
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert len(synth.inputs) == 5
        assert playback.played
        assert storage.plan(narration_id).settings.prepare_first is True
        assert not storage.plan(narration_id).gap_ordinals
    finally:
        worker.close()


def parked(playback: PositionedPlayback) -> None:
    """Wait until preparation has sat at the lookahead gate, not merely paused.

    The generation thread reads the playhead only from inside the gate, so
    a second read means it reached the gate and stayed long enough to poll
    again. That is the wait; the assertion each caller makes next is what
    proves nothing further was prepared in the meantime.
    """
    polls = playback.lookahead_polls
    wait_until(lambda: playback.lookahead_polls >= polls + 2)


@pytest.mark.parametrize("speed,ready_blocks", [(1.0, 3), (2.0, 6), (4.0, 9)])
def test_budget_uses_listening_seconds_and_reopens_when_the_playhead_moves(
    tmp_path, speed, ready_blocks
):
    synth, playback = FixedSynthesizer(20.0), PositionedPlayback(0.0)
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, playback, storage)
    try:
        worker.set_speed(speed)
        narration_id = worker.start(
            request("\n\n".join(f"Paragraph {i}." for i in range(10)))
        )
        wait_until(lambda: len(synth.inputs) == ready_blocks)
        parked(playback)
        assert len(synth.inputs) == ready_blocks
        assert (
            sum(s.audio_present for s in storage.history_detail(narration_id).segments)
            == ready_blocks
        )
        playback.position_sec = 200.0
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert len(synth.inputs) == 10
    finally:
        worker.close()


def test_cached_audio_beyond_a_hole_does_not_delay_synthesizing_the_hole(tmp_path):
    release = threading.Event()
    playback = HeldPlayback(release)
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("First.\n\nHole.\n\nCached.\n\nLater.", KOKORO),
        SETTINGS,
        entry_for(SETTINGS),
    )
    publish_segment(
        storage, plan, plan.segments[0], np.ones(240, dtype=np.float32), 24_000
    )
    publish_segment(
        storage, plan, plan.segments[2], np.ones(80 * 24_000, dtype=np.float32), 24_000
    )
    storage.set_status(plan.id, NarrationStatus.STOPPED)
    synth = RecordingSynthesizer()
    worker = worker_for(synth, playback, storage)
    try:
        worker.resume(plan.id)
        assert playback.entered.wait(timeout=2)
        wait_until(lambda: storage.history_detail(plan.id).segments[1].audio_present)
        parked(playback)
        assert synth.inputs == ["Hole."]
    finally:
        release.set()
        worker.close()


def test_live_speed_reopens_lookahead_without_restarting_or_rekeying(tmp_path):
    synth, playback = FixedSynthesizer(20.0), PositionedPlayback(0.0)
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(
            request("\n\n".join(f"Paragraph {i}." for i in range(10)))
        )
        parked(playback)
        wait_until(lambda: worker.snapshot()["phase"] == "playing")
        before = storage.plan(narration_id)
        stops = playback.stop_calls
        worker.pause()
        worker.set_speed(3.0)
        wait_until(lambda: len(synth.inputs) == 9)
        parked(playback)
        assert len(synth.inputs) == 9
        assert playback.speed == 3.0
        assert playback.stop_calls == stops
        assert worker.snapshot()["phase"] == "paused"
        assert worker.snapshot()["speed"] == 3.0
        assert storage.playback_speed() == 3.0
        assert [s.key for s in storage.plan(narration_id).segments] == [
            s.key for s in before.segments
        ]
    finally:
        worker.close()
        storage.close()


def test_a_restarted_engine_resumes_at_the_persisted_speed(tmp_path):
    storage = open_test_storage(tmp_path)
    storage.set_playback_speed(3.0)
    playback = RecordingPlayback()
    worker = worker_for(RecordingSynthesizer(), playback, storage)
    try:
        assert worker.snapshot()["speed"] == 3.0
        assert playback.speed == 3.0
        new_id = worker.start(request("Another."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert storage.plan(new_id).settings.speed == 3.0
    finally:
        worker.close()
        storage.close()


def test_seek_to_uncached_block_preempts_old_work_then_prepares_forward(tmp_path):
    release = threading.Event()
    feed_release = threading.Event()
    playback = HeldPlayback(feed_release)
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("One.\n\nTwo.\n\nThree.\n\nFour.\n\nFive.\n\nSix.\n\nSeven.", KOKORO),
        SETTINGS,
        entry_for(SETTINGS),
    )
    for segment in plan.segments:
        publish_segment(
            storage, plan, segment, np.ones(20 * 24_000, dtype=np.float32), 24_000
        )
    storage.update_retention(segment_budget_bytes=1, keep_audio_days=None)
    assert not storage.history_detail(plan.id).audio_present
    storage.set_status(plan.id, NarrationStatus.STOPPED)
    synth = FixedSynthesizer(20.0, hold=release, hold_text="One.")
    worker = worker_for(synth, playback, storage)
    try:
        worker.resume(plan.id)
        wait_until(lambda: synth.inputs == ["One."])
        assert worker.seek(plan.segments[3].source_start)
        release.set()
        wait_until(lambda: storage.history_detail(plan.id).segments[5].audio_present)
        parked(playback)
        assert synth.inputs == ["One.", "Four.", "Five.", "Six."]
        assert not storage.history_detail(plan.id).segments[0].audio_present
    finally:
        release.set()
        feed_release.set()
        worker.close()


@pytest.mark.parametrize(
    "source,saved_frames,total_sec",
    [("Regenerate me.", 24_000, 2.0), ("Repeat.\n\nRepeat.", 48_000, 4.4)],
)
def test_regenerated_blocks_play_and_remain_seekable_after_eviction(
    tmp_path, source, saved_frames, total_sec
):
    storage = open_test_storage(tmp_path)
    plan = storage.create(chunk(source, KOKORO), SETTINGS, entry_for(SETTINGS))
    for segment in plan.segments:
        publish_segment(
            storage, plan, segment, np.ones(saved_frames, dtype=np.float32), 24_000
        )
    storage.update_retention(segment_budget_bytes=1, keep_audio_days=None)
    storage.set_status(plan.id, NarrationStatus.STOPPED)
    playback = RecordingPlayback()
    worker = worker_for(FixedSynthesizer(2.0), playback, storage)
    try:
        worker.resume(plan.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert sum(len(pcm) for pcm, _ in playback.played) == round(total_sec * 24_000)
        for segment in storage.history_detail(plan.id).segments:
            assert segment.duration_sec == 2.0
        assert None not in timeline_starts(storage.plan(plan.id))
        assert worker.snapshot()["totalSec"] == total_sec
    finally:
        worker.close()


def test_shared_narration_recovers_lengths_when_evicted_audio_is_regenerated(tmp_path):
    storage = open_test_storage(tmp_path)
    source = chunk("Shared audio.", KOKORO)
    first = storage.create(source, SETTINGS, entry_for(SETTINGS))
    second = storage.create(source, SETTINGS, entry_for(SETTINGS))
    publish_segment(
        storage, first, first.segments[0], np.ones(24_000, dtype=np.float32), 24_000
    )
    trimmed_audio(storage, second, second.segments[0])
    stale_plan = storage.plan(second.id)
    storage.update_retention(segment_budget_bytes=1, keep_audio_days=None)
    publish_segment(
        storage, first, first.segments[0], np.ones(48_000, dtype=np.float32), 24_000
    )

    assert storage.plan(second.id).segments[0].frame_count is None
    assert len(storage.raw_audio(stale_plan.segments[0]).pcm) == 48_000
    recovered = recover_lengths(storage, storage.plan(second.id))
    assert recovered.segments[0].frame_count == 48_000
    assert storage.history_detail(second.id).segments[0].duration_sec == 2.0


def test_preparation_failure_cancels_a_feeder_blocked_on_the_device_ring(
    tmp_path, monkeypatch
):
    second_feed = threading.Event()
    recorder = Recorder()
    playback = recorder.player(clock=lambda: 0.0)
    wait_for = playback._wait_for

    def observe_backpressure(ready, generation):
        if not ready():
            second_feed.set()
        return wait_for(ready, generation)

    monkeypatch.setattr(playback, "_wait_for", observe_backpressure)

    class FailedPreparation(RecordingStorage):
        def raw_audio(self, segment):
            if self.ordinals[segment.key] == 2:
                assert second_feed.wait(timeout=2)
                raise RuntimeError("Preparation failed with a full ring")
            return self.wrapped.raw_audio(segment)

    worker = worker_for(
        FixedSynthesizer(20.0, value=0.25),
        playback,
        FailedPreparation(open_test_storage(tmp_path)),
    )
    try:
        worker.start(request("First.\n\nSecond.\n\nThird."))
        wait_until(lambda: worker.snapshot()["phase"] == "failed")
        time.sleep(0.1)
        assert playback._ring.empty()
        assert not recorder.streams[0].callback_once().any()
    finally:
        worker.close()
        playback.close()
