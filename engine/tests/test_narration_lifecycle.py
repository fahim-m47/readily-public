"""Failure, resume, shutdown, and the races between them."""

import threading
import time
from datetime import UTC, datetime

import numpy as np
import pytest
from conftest import wait_until
from storage_fakes import AdvancingClock, create_plan, encode_npz, open_test_storage
from worker_fakes import (
    FailingStorage,
    HoldSecondBlockSynthesizer,
    PositionedPlayback,
    RecordingPlayback,
    RecordingStorage,
    RecordingSynthesizer,
    request,
    worker_for,
)

from readily_engine.audio import AudioOutputUnavailable
from readily_engine.generation import GeneratedAudio
from readily_engine.narration import worker as worker_module
from readily_engine.narration.timeline import timeline_starts
from readily_engine.storage.history import NarrationStatus
from readily_engine.storage.storage import NarrationNotResumable, NarrationStorage


@pytest.mark.parametrize("damage", ["replaced", "digestless"])
def test_replay_regenerates_a_measured_segment_whose_bytes_are_not_verified(
    tmp_path, damage
):
    synth = RecordingSynthesizer()
    storage = open_test_storage(tmp_path)
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(request("Read again."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        audio_file = next((tmp_path / "segments").rglob("*.npz"))
        if damage == "replaced":
            encode_npz(np.zeros(100, dtype=np.float32), 24_000, audio_file)
        else:
            audio_file.with_suffix(".frames").write_text("240\n")

        worker.resume(narration_id)
        wait_until(lambda: worker.snapshot()["phase"] in {"finished", "failed"})

        assert worker.snapshot()["phase"] == "finished"
        assert worker.snapshot()["error"] is None
        assert synth.inputs == ["Read again.", "Read again."]
        assert len(playback.played) == 2
        assert storage.history_detail(narration_id).audio_present
    finally:
        worker.close()
        storage.close()


def test_a_block_that_fails_twice_becomes_a_flagged_gap_and_playback_continues(
    tmp_path,
):
    class FirstBlockBrokenSynthesizer:
        def __init__(self) -> None:
            self.inputs: list[str] = []

        def generate(self, record):
            text = record.text
            self.inputs.append(text)
            if text == "First.":
                raise RuntimeError("/private/path/model.onnx exploded")
            return GeneratedAudio(np.ones(480, dtype=np.float32), 24_000)

    synth = FirstBlockBrokenSynthesizer()
    storage = open_test_storage(tmp_path)
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(request("First. Second."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")

        # Retried once, then skipped: the second Block still played.
        assert synth.inputs == ["First.", "First.", "Second."]
        assert len(playback.played) == 1
        assert worker.snapshot()["error"]["code"] == "generation_gap"
        assert "/private/path" not in str(worker.snapshot())

        detail = storage.history_detail(narration_id)
        assert detail.status is NarrationStatus.FINISHED
        assert detail.has_gaps is True
        assert [(gap.ordinal, gap.error_code) for gap in detail.gaps] == [
            (0, "generation_failed")
        ]
    finally:
        worker.close()


def test_a_block_that_fails_once_is_retried_and_leaves_no_gap(tmp_path):
    class FlakySynthesizer(RecordingSynthesizer):
        def generate(self, record):
            text = record.text
            self.inputs.append(text)
            if self.inputs.count(text) == 1:
                raise RuntimeError("transient")
            return GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)

    synth = FlakySynthesizer()
    storage = open_test_storage(tmp_path)
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(request("Flaky."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")

        assert synth.inputs == ["Flaky.", "Flaky."]
        assert len(playback.played) == 1
        assert storage.history_detail(narration_id).has_gaps is False
    finally:
        worker.close()


def test_an_unexpected_error_outside_synthesis_still_fails_the_narration(tmp_path):
    class BrokenStorage(RecordingStorage):
        def raw_audio(self, segment):
            raise RuntimeError("/private/db exploded")

    storage = BrokenStorage(open_test_storage(tmp_path))
    worker = worker_for(RecordingSynthesizer(), RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("broken"))
        wait_until(lambda: worker.snapshot()["phase"] == "failed")

        assert worker.snapshot()["error"] == {
            "version": 1,
            "code": "generation_failed",
            "message": "The Engine could not generate this Narration.",
        }
        assert "/private/db" not in str(worker.snapshot())
        assert storage.history_detail(narration_id).status is NarrationStatus.FAILED
    finally:
        worker.close()


def test_a_generic_failure_silences_audio_that_was_already_buffered(tmp_path):
    playback = RecordingPlayback()

    class BrokenSecondRead(RecordingStorage):
        def __init__(self, wrapped: NarrationStorage) -> None:
            super().__init__(wrapped)

        def raw_audio(self, segment):
            if self.ordinals[segment.key] == 1:
                wait_until(lambda: len(playback.played) == 1)
                raise RuntimeError("second Block read failed")
            return self.wrapped.raw_audio(segment)

    storage = BrokenSecondRead(open_test_storage(tmp_path))
    worker = worker_for(RecordingSynthesizer(), playback, storage)
    try:
        worker.start(request("First.\n\nSecond."))
        wait_until(lambda: worker.snapshot()["phase"] == "failed")

        assert len(playback.played) == 1
        assert playback.stop_calls == 2
        assert playback.generation == 2
    finally:
        worker.close()


def test_close_reports_a_worker_still_inside_a_native_call(tmp_path, monkeypatch):
    # An interrupt cannot enter a native call, which the held synthesizer
    # stands in for, so a synthesis that has not come back cannot be waited
    # out. `close` says so rather than pretending the thread stopped — the
    # caller needs that answer to decide whether tearing down the audio
    # device is safe.
    monkeypatch.setattr(worker_module, "SHUTDOWN_GRACE_SECONDS", 0.05)
    release = threading.Event()
    synth = RecordingSynthesizer(hold=release)
    worker = worker_for(synth, RecordingPlayback(), open_test_storage(tmp_path))
    try:
        worker.start(request("stuck in synthesis"))
        wait_until(lambda: synth.inputs == ["stuck in synthesis"])

        assert worker.close() is False
        assert worker.thread.is_alive()
    finally:
        release.set()
        worker.thread.join(timeout=2)


def test_close_reports_a_worker_that_stopped(tmp_path):
    worker = worker_for(
        RecordingSynthesizer(), RecordingPlayback(), open_test_storage(tmp_path)
    )
    worker.start(request("done"))
    wait_until(lambda: worker.snapshot()["phase"] == "finished")

    assert worker.close() is True
    assert not worker.thread.is_alive()


def test_stop_keeps_stopped_when_synthesis_then_fails(tmp_path):
    release = threading.Event()

    class ExplodingSynthesizer(RecordingSynthesizer):
        def generate(self, record):
            text = record.text
            self.inputs.append(text)
            if text == "First.":
                return GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)
            if self.hold is not None:
                self.hold.wait(timeout=2)
            raise RuntimeError("synthesis exploded")

    synth = ExplodingSynthesizer(hold=release)
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("First. Exploding."))
        wait_until(lambda: synth.inputs == ["First.", "Exploding."])
        assert worker.stop() is True
        assert storage.history_detail(narration_id).status is NarrationStatus.STOPPED
        release.set()
        with pytest.raises(AssertionError, match="condition was not reached"):
            wait_until(
                lambda: (
                    storage.history_detail(narration_id).status
                    is NarrationStatus.FAILED
                ),
                timeout=0.4,
            )
        assert storage.history_detail(narration_id).status is NarrationStatus.STOPPED
    finally:
        release.set()
        worker.close()


def test_resume_marks_preparing_so_a_second_resume_is_rejected(tmp_path):
    hold = threading.Event()
    storage = open_test_storage(tmp_path)
    plan = create_plan(storage, "Held.")
    storage.set_status(plan.id, NarrationStatus.INTERRUPTED)
    worker = worker_for(RecordingSynthesizer(hold=hold), RecordingPlayback(), storage)
    try:
        worker.resume(plan.id)
        wait_until(
            lambda: storage.history_detail(plan.id).status is NarrationStatus.PREPARING
        )
        with pytest.raises(NarrationNotResumable):
            worker.resume(plan.id)
    finally:
        hold.set()
        worker.close()


def test_racing_resumes_admit_the_narration_once(tmp_path):
    """Eligibility and PREPARING must be one durable-state operation.

    Both callers can otherwise read STOPPED before either one writes
    PREPARING, so the same Narration is accepted twice.
    """

    class PausingFirstResume(RecordingStorage):
        def __init__(self, wrapped: NarrationStorage) -> None:
            super().__init__(wrapped)
            self.first_read = threading.Event()
            self.second_read = threading.Event()
            self.calls = 0
            self.calls_lock = threading.Lock()

        def resume(self, narration_id: str):
            plan = self.wrapped.resume(narration_id)
            with self.calls_lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                self.first_read.set()
                self.second_read.wait(timeout=0.5)
            else:
                self.second_read.set()
            return plan

    hold = threading.Event()
    base = open_test_storage(tmp_path)
    plan = create_plan(base, "Held.")
    base.set_status(plan.id, NarrationStatus.STOPPED)
    storage = PausingFirstResume(base)
    worker = worker_for(RecordingSynthesizer(hold=hold), RecordingPlayback(), storage)
    accepted: list[str] = []
    rejected: list[Exception] = []

    def resume() -> None:
        try:
            accepted.append(worker.resume(plan.id))
        except Exception as error:
            rejected.append(error)

    first = threading.Thread(target=resume)
    second = threading.Thread(target=resume)
    try:
        first.start()
        assert storage.first_read.wait(timeout=1)
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert accepted == [plan.id]
        assert len(rejected) == 1
        assert isinstance(rejected[0], NarrationNotResumable)
    finally:
        hold.set()
        worker.close()


def test_close_of_an_active_job_persists_interrupted(tmp_path):
    release = threading.Event()
    synth = RecordingSynthesizer(hold=release)
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("closing"))
        wait_until(lambda: synth.inputs == ["closing"])
        worker.close()
        detail = storage.history_detail(narration_id)
        assert detail.status is NarrationStatus.INTERRUPTED
    finally:
        release.set()
        worker.thread.join(timeout=2)


def test_finished_checkpoint_equals_total_duration(tmp_path):
    storage = open_test_storage(tmp_path)
    worker = worker_for(RecordingSynthesizer(), RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("Done."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        detail = storage.history_detail(narration_id)
        assert detail.status is NarrationStatus.FINISHED
        assert detail.playhead_sec == pytest.approx(0.01)
        assert detail.total_duration_sec == pytest.approx(0.01)
    finally:
        worker.close()


def test_deleting_the_active_narration_cancels_generation_before_gc(tmp_path):
    release = threading.Event()
    synth = RecordingSynthesizer(hold=release)
    storage = open_test_storage(tmp_path)
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(request("Delete while generating."))
        wait_until(lambda: synth.inputs == ["Delete while generating."])

        result = worker.delete(narration_id)

        assert result is not None
        assert worker.snapshot()["phase"] == "idle"
        assert storage.history_detail(narration_id) is None
        release.set()
        wait_until(lambda: synth.active == 0)
        assert storage.retention().audio_bytes == 0
        assert playback.played == []
    finally:
        release.set()
        worker.close()


def test_checkpoint_loop_persists_callback_position_off_the_audio_thread(tmp_path):
    storage = RecordingStorage(open_test_storage(tmp_path))
    playback = PositionedPlayback(position_sec=1.25)
    worker = worker_for(
        RecordingSynthesizer(hold=threading.Event()),
        playback,
        storage,
        checkpoint_seconds=0.01,
    )
    try:
        narration_id = worker.start(request("Checkpoint."))
        wait_until(lambda: storage.checkpoints)
        assert storage.checkpoints[-1] == (narration_id, pytest.approx(1.25))
        assert storage.checkpoint_thread_ids[-1] == playback.position_thread_ids[-1]
        assert storage.checkpoint_thread_ids[-1] != worker.thread.ident
    finally:
        worker.close()


def test_the_worker_thread_survives_a_narration_whose_failure_cannot_be_recorded(
    tmp_path,
):
    """The recording of a failure writes to the storage that just failed.
    If that write can end the thread, the Engine keeps accepting 202s into
    a queue with no consumer and every later Narration hangs in preparing."""
    storage = FailingStorage(
        open_test_storage(tmp_path), failing={"raw_audio", "checkpoint"}
    )
    worker = worker_for(RecordingSynthesizer(), RecordingPlayback(), storage)
    try:
        worker.start(request("Doomed."))
        wait_until(lambda: worker.snapshot()["phase"] == "failed")
        assert worker.thread.is_alive()

        storage.failing.clear()
        worker.start(request("Afterwards."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
    finally:
        worker.close()


def test_the_worker_thread_survives_a_failure_nothing_anticipated(tmp_path):
    """The worker thread is never replaced, so its survival cannot depend
    on having predicted which step throws. Here the audio backend throws
    from the very call the failure path uses to find the playhead."""

    class ExplodingPosition(RecordingPlayback):
        def __init__(self) -> None:
            super().__init__()
            self.exploding = True
            self.explosions = 0

        def position(self, generation: int) -> float:
            if self.exploding:
                self.explosions += 1
                raise RuntimeError("the audio backend is gone")
            return super().position(generation)

    playback = ExplodingPosition()
    worker = worker_for(RecordingSynthesizer(), playback, open_test_storage(tmp_path))
    try:
        worker.start(request("Doomed."))
        # Twice: once on the way through the Narration, once more on the
        # failure path that was supposed to make sense of the first.
        wait_until(lambda: playback.explosions >= 2)
        wait_until(lambda: worker.snapshot()["phase"] == "failed")
        assert worker.thread.is_alive()

        playback.exploding = False
        worker.start(request("Afterwards."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
    finally:
        playback.exploding = False
        worker.close()


def test_the_checkpoint_thread_survives_a_transient_storage_failure(tmp_path):
    storage = FailingStorage(open_test_storage(tmp_path), failing={"checkpoint"})
    worker = worker_for(
        RecordingSynthesizer(hold=threading.Event()),
        PositionedPlayback(position_sec=1.25),
        storage,
        checkpoint_seconds=0.01,
    )
    try:
        narration_id = worker.start(request("Checkpoint."))
        wait_until(lambda: storage.attempts.count("checkpoint") >= 3)

        storage.failing.clear()
        wait_until(lambda: storage.checkpoints)
        assert storage.checkpoints[-1] == (narration_id, pytest.approx(1.25))
    finally:
        worker.close()


def test_a_paused_narration_stops_rewriting_the_same_playhead(tmp_path):
    """The checkpoint loop runs for the Engine's whole life. A pause can
    last an afternoon, and a playhead that has not moved is not news."""
    release = threading.Event()
    playback = RecordingPlayback()
    storage = RecordingStorage(open_test_storage(tmp_path))
    worker = worker_for(HoldSecondBlockSynthesizer(release), playback, storage, 0.01)
    try:
        worker.start(request("First. Second."))
        # The phase turns `playing` just before the first Block is fed, so
        # pausing on the phase alone can freeze a playhead that is still
        # about to move — and the one write that move earns is exactly the
        # rewrite this test is watching for. Wait for the audio, pause, and
        # take the baseline only once a checkpoint has recorded the frozen
        # position; from there any further write is a real rewrite.
        wait_until(lambda: playback.played)
        assert worker.pause() is True
        frozen = playback.position(playback.generation)
        wait_until(
            lambda: (
                storage.checkpoints
                and storage.checkpoints[-1][1] == pytest.approx(frozen)
            )
        )

        written = len(storage.checkpoints)
        time.sleep(0.15)  # roughly fifteen checkpoint ticks

        assert len(storage.checkpoints) == written
    finally:
        release.set()
        worker.close()


def test_a_narration_the_output_device_swallowed_is_interrupted_not_finished(tmp_path):
    class DeadOutput(RecordingPlayback):
        """A device lost after the first audible Block."""

        def feed(
            self, pcm, sample_rate: int, generation: int, *, pause_frames: int = 0
        ) -> float:
            if self.played:
                raise AudioOutputUnavailable
            return super().feed(pcm, sample_rate, generation)

    playback = DeadOutput()
    storage = open_test_storage(tmp_path)
    synth = RecordingSynthesizer()
    worker = worker_for(synth, playback, storage)
    try:
        narration_id = worker.start(request("One.\n\nTwo.\n\nThree.\n\nFour.\n\nFive."))
        wait_until(lambda: worker.snapshot()["phase"] == "failed")

        # The worker stopped at the dead device instead of synthesizing the
        # rest of the Narration into it. One Block can already be in flight
        # when the output operation reports the loss.
        assert len(synth.inputs) <= 3
        snapshot = worker.snapshot()
        assert snapshot["error"]["code"] == "audio_output_unavailable"
        assert snapshot["positionSec"] == pytest.approx(0.01)
        detail = storage.history_detail(narration_id)
        # Interrupted, not finished: finished is the one status Resume
        # refuses, and nothing past the first Block was ever audible.
        assert detail.status is NarrationStatus.INTERRUPTED
        assert detail.playhead_sec == pytest.approx(0.01)
        assert detail.total_duration_sec is None
    finally:
        worker.close()


def test_a_narration_whose_device_dies_during_the_drain_is_not_finished(tmp_path):
    """`drain` returns as soon as the ring is empty, and writing the output
    off empties it by throwing the queue away. Every Block fed is not every
    Block heard."""

    class DiesOnDrain(RecordingPlayback):
        def drain(self, generation: int) -> float:
            raise AudioOutputUnavailable

    storage = open_test_storage(tmp_path)
    worker = worker_for(RecordingSynthesizer(), DiesOnDrain(), storage)
    try:
        narration_id = worker.start(request("Drained."))
        wait_until(lambda: worker.snapshot()["phase"] == "failed")

        assert (
            storage.history_detail(narration_id).status is NarrationStatus.INTERRUPTED
        )
    finally:
        worker.close()


def test_a_stop_that_races_the_first_feed_leaves_the_narration_stopped(tmp_path):
    """`stop` and the worker's `playing` write touch the same row from two
    threads. If the `playing` write can land after `stopped`, the History
    claims a Narration is playing while the Engine sits idle, and Resume
    rejects it until the next restart's sweep."""
    reached_playing = threading.Event()
    wrote_playing = threading.Event()
    release = threading.Event()

    class SlowPlayingWrite(RecordingStorage):
        """Holds the `playing` write open, which is the window a Stop from
        the HTTP thread lands in on a slow disk."""

        def set_status(self, narration_id, status, **changes):
            if status is NarrationStatus.PLAYING:
                reached_playing.set()
                release.wait(timeout=2)
            self.wrapped.set_status(narration_id, status, **changes)
            if status is NarrationStatus.PLAYING:
                wrote_playing.set()

    storage = SlowPlayingWrite(open_test_storage(tmp_path))
    playback = RecordingPlayback()
    worker = worker_for(RecordingSynthesizer(), playback, storage)
    try:
        narration_id = worker.start(request("Raced."))
        assert reached_playing.wait(timeout=2)
        admitted_stop_calls = playback.stop_calls

        stopper = threading.Thread(target=worker.stop)
        stopper.start()
        wait_until(lambda: playback.stop_calls == admitted_stop_calls + 1, timeout=0.4)
        assert not wrote_playing.is_set()
        assert stopper.is_alive()  # durable STOPPED still waits behind PLAYING
        release.set()
        stopper.join(timeout=2)
        assert wrote_playing.wait(timeout=2)

        wait_until(lambda: worker.snapshot()["phase"] == "idle")
        assert storage.history_detail(narration_id).status is NarrationStatus.STOPPED
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize(
    ("terminal_status", "terminal_phase", "error_code"),
    [
        (NarrationStatus.FINISHED, "finished", None),
        (NarrationStatus.FAILED, "failed", "generation_failed"),
        (
            NarrationStatus.INTERRUPTED,
            "failed",
            "audio_output_unavailable",
        ),
    ],
)
def test_terminal_outcome_is_published_before_a_racing_stop(
    tmp_path,
    terminal_status,
    terminal_phase,
    error_code,
):
    """Once a terminal write lands, Stop must observe the terminal phase.

    Otherwise it can see the old active phase in the gap after the database
    write, return success, and replace FINISHED/FAILED/INTERRUPTED with
    STOPPED even though the terminal outcome already happened.
    """
    terminal_written = threading.Event()
    release_terminal = threading.Event()
    stop_started = threading.Event()
    seek_started = threading.Event()

    class BlockingTerminalStorage(RecordingStorage):
        def raw_audio(self, segment):
            if terminal_status is NarrationStatus.FAILED:
                raise RuntimeError("storage read failed")
            return self.wrapped.raw_audio(segment)

        def set_status(self, narration_id, status, **changes):
            self.wrapped.set_status(narration_id, status, **changes)
            if status is terminal_status:
                terminal_written.set()
                release_terminal.wait(timeout=2)

    class LostOutput(RecordingPlayback):
        def feed(
            self, pcm, sample_rate: int, generation: int, *, pause_frames: int = 0
        ) -> float:
            if terminal_status is NarrationStatus.INTERRUPTED:
                raise AudioOutputUnavailable
            return super().feed(pcm, sample_rate, generation)

    storage = BlockingTerminalStorage(open_test_storage(tmp_path))
    worker = worker_for(RecordingSynthesizer(), LostOutput(), storage)
    stopped: list[bool] = []
    seeked: list[bool] = []

    def stop() -> None:
        stop_started.set()
        stopped.append(worker.stop())

    def seek() -> None:
        seek_started.set()
        seeked.append(worker.seek(0.0))

    stopper = threading.Thread(target=stop)
    seeker = threading.Thread(target=seek)
    try:
        narration_id = worker.start(request("Terminal."))
        assert terminal_written.wait(timeout=2)
        seeker.start()
        assert seek_started.wait(timeout=1)
        wait_until(lambda: seeked == [False], timeout=0.4)
        stopper.start()
        assert stop_started.wait(timeout=1)
        wait_until(lambda: stopped == [False], timeout=0.4)
        # The internal terminal reservation keeps controls responsive, but
        # the observable phase does not claim durability before the blocked
        # History write has returned.
        assert worker.snapshot()["phase"] in {"preparing", "playing", "paused"}
        release_terminal.set()
        stopper.join(timeout=2)

        assert not stopper.is_alive()
        assert stopped == [False]
        wait_until(lambda: worker.snapshot()["phase"] == terminal_phase)
        assert (
            None
            if worker.snapshot()["error"] is None
            else worker.snapshot()["error"]["code"]
        ) == error_code
        assert storage.history_detail(narration_id).status is terminal_status
    finally:
        release_terminal.set()
        seeker.join(timeout=2)
        if stop_started.is_set():
            stopper.join(timeout=2)
        worker.close()


def test_a_block_that_succeeds_on_a_later_run_stops_being_a_gap(tmp_path):
    """A gap is a claim about the audio, so audio has to be able to retract it."""

    class BrokenUntilResumed:
        def __init__(self) -> None:
            self.inputs: list[str] = []

        def generate(self, record):
            text = record.text
            self.inputs.append(text)
            # Both attempts of the first run fail; the replay succeeds.
            if text == "First." and self.inputs.count("First.") <= 2:
                raise RuntimeError("the model was not loaded")
            return GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)

    synth = BrokenUntilResumed()
    storage = open_test_storage(tmp_path)
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("First. Second."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert storage.history_detail(narration_id).has_gaps is True

        worker.resume(narration_id)
        wait_until(
            lambda: (
                storage.history_detail(narration_id).status is NarrationStatus.FINISHED
                and worker.snapshot()["phase"] == "finished"
            )
        )

        detail = storage.history_detail(narration_id)
        assert detail.gaps == ()
        assert detail.has_gaps is False
        assert detail.audio_present is True
        assert all(segment.audio_present for segment in detail.segments)
    finally:
        worker.close()


def test_a_block_that_cannot_be_stored_fails_the_narration(tmp_path):
    """Publication is storage, not synthesis: it must not become a gap.

    A gap says the words were never spoken. Audio that synthesized fine and
    could not be written says the opposite — and reporting `finished` with
    no error would leave the listener with a silent stretch and nothing in
    History to explain it.
    """

    class UnwritableStorage(RecordingStorage):
        def store_audio(self, key, pcm, sample_rate):
            raise OSError(28, "No space left on device")

    synth = RecordingSynthesizer()
    storage = UnwritableStorage(open_test_storage(tmp_path))
    worker = worker_for(synth, RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request("First. Second."))
        wait_until(lambda: worker.snapshot()["phase"] == "failed")

        assert worker.snapshot()["error"]["code"] == "generation_failed"
        # One attempt, not two: a failed write is not a reason to re-run a
        # synthesizer that already did its job.
        assert synth.inputs == ["First."]

        detail = storage.history_detail(narration_id)
        assert detail.status is NarrationStatus.FAILED
        assert detail.gaps == ()
        assert detail.has_gaps is False
    finally:
        worker.close()


def test_a_stop_survives_the_narration_being_deleted_underneath_it(tmp_path):
    """Stop captures its outcome under one lock and writes it under another.

    A delete landing in that window leaves no row to write the playhead to.
    The listener asked for the Narration to stop and for it to be gone, and
    both happened — so Stop reports success rather than surfacing the lost
    write to the client as a 500.
    """

    class DeletingStorage(RecordingStorage):
        def __init__(self, wrapped) -> None:
            super().__init__(wrapped)
            self.deleted = False

        def checkpoint(self, narration_id: str, position_sec: float) -> None:
            if not self.deleted:
                self.deleted = True
                self.wrapped.delete(narration_id)
            super().checkpoint(narration_id, position_sec)

    release = threading.Event()
    synth = HoldSecondBlockSynthesizer(release)
    storage = DeletingStorage(open_test_storage(tmp_path))
    worker = worker_for(synth, RecordingPlayback(), storage, checkpoint_seconds=60.0)
    try:
        narration_id = worker.start(request("First. Second."))
        wait_until(lambda: worker.snapshot()["phase"] == "playing")

        assert worker.stop() is True
        assert storage.deleted is True
        assert worker.snapshot()["phase"] == "idle"
        assert storage.history_detail(narration_id) is None
    finally:
        release.set()
        worker.close()


def test_resuming_evicted_audio_synthesizes_only_from_the_playhead(tmp_path):
    """Retention deletes audio; Resume must not quietly re-create all of it.

    The Blocks before the playhead were heard on the previous run and their
    audio has since been swept. Re-synthesizing them to reach the playhead
    would cost minutes of work and the battery to match, for sound nobody
    hears — so Resume opens on the Block the playhead is in.
    """
    clock = AdvancingClock(datetime(2026, 8, 1, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    first_synth = RecordingSynthesizer()
    worker = worker_for(first_synth, RecordingPlayback(), storage)
    try:
        # Paragraph breaks, so each sentence is a Block of its own.
        narration_id = worker.start(request("One.\n\nTwo.\n\nThree.\n\nFour."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert first_synth.inputs == ["One.", "Two.", "Three.", "Four."]
    finally:
        worker.close()

    # Where the third Block actually starts, read back from the run that
    # played it — the assembled clock, not a sum of Segment durations.
    offsets = timeline_starts(storage.resume(narration_id))
    assert all(offset is not None for offset in offsets)

    clock.advance(seconds=8 * 24 * 60 * 60)
    storage.update_retention(
        segment_budget_bytes=storage.retention().segment_budget_bytes,
        keep_audio_days=7,
    )
    assert storage.history_detail(narration_id).audio_present is False
    storage.set_status(
        narration_id,
        NarrationStatus.STOPPED,
        playhead_sec=offsets[2] + 0.001,
    )

    synth = RecordingSynthesizer()
    playback = RecordingPlayback()
    worker = worker_for(synth, playback, storage)
    try:
        worker.resume(narration_id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")

        assert synth.inputs == ["Three.", "Four."]
        assert playback.played
        # The Narration still measures its own full length, not the tail
        # this run happened to generate.
        assert storage.history_detail(narration_id).total_duration_sec > offsets[2]
    finally:
        worker.close()
