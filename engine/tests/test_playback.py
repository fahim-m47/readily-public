"""Native-rate playback keeps synthesis work out of the realtime callback."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import wait_until

from readily_engine.audio import AudioOutputUnavailable
from readily_engine.audio.playback import (
    BLOCKSIZE,
    LATENCY_SECONDS,
    MAX_BUFFERED_SECONDS,
    MAX_STALL_RECOVERIES,
    PROCESS_SECONDS,
    STALL_SECONDS,
    OutputDevice,
    PcmRingBuffer,
    SoundDevicePlayback,
)
from readily_engine.audio.resampling import resample_poly
from readily_engine.audio.stretch import TimedPcm

BUILT_IN = OutputDevice(key="built-in", sample_rate=48_000)


def frames(count: int) -> TimedPcm:
    return TimedPcm(np.ones(count, dtype=np.float32), count)


def prefill(player: SoundDevicePlayback, count: int) -> None:
    """Open the stream and queue `count` native frames directly. No
    callback fires in these tests, so `feed` would block on a full ring."""
    player._ensure_stream()
    player._ring.push(frames(count))


def test_starvation_clears_when_audio_arrives_and_excludes_pauses_and_replacement():
    recorder = Recorder()
    player = recorder.player()
    player.set_speed(4)
    try:
        player.feed(np.full(10, 0.25, dtype=np.float32), 24_000, 0)
        recorder.streams[0].callback_once(4096)
        assert not player.starving(0)
        player.feed(np.full(12_000, 0.25, dtype=np.float32), 24_000, 0)
        recorder.streams[0].callback_once(48_000)
        assert player.starving(0)
        player.pause(0)
        assert not player.starving(0)
        player.unpause(0)
        assert player.starving(0)
        player.feed(np.full(4800, 0.25, dtype=np.float32), 24_000, 0)
        assert not player.starving(0)
        recorder.streams[0].callback_once(48_000)
        assert player.starving(0)
        player.stop(1)
        assert not player.starving(0)
        assert not player.starving(1)
    finally:
        player.close()


def test_live_speed_changes_advance_the_source_clock_without_reopening_output():
    recorder = Recorder()
    player = recorder.player()
    pcm = np.full(1200, 0.25, dtype=np.float32)
    try:
        for _ in range(20):
            player.feed(pcm, 48_000, 0)
            recorder.streams[0].callback_once(2400)
        assert player.position(0) == pytest.approx(0.5, abs=0.01)
        player.set_speed(2.0)
        for _ in range(40):
            player.feed(pcm, 48_000, 0)
            recorder.streams[0].callback_once(2400)
        assert player.position(0) == pytest.approx(1.5, abs=0.1)
        assert len(recorder.streams) == 1
        player.stop(1)
        assert player.position(1) == 0
    finally:
        player.close()


@pytest.mark.parametrize("device_rate", [24_000, 44_100, 48_000])
def test_four_times_shortens_only_authored_pauses_and_keeps_source_time(device_rate):
    rate = 24_000
    tone = (0.4 * np.sin(2 * np.pi * 200 * np.arange(rate * 2) / rate)).astype(
        np.float32
    )
    speech = np.concatenate([tone, np.zeros(rate, dtype=np.float32), tone])

    def render(speed):
        recorder = Recorder(OutputDevice("test", device_rate))
        player = recorder.player()
        player.set_speed(speed)
        done = threading.Event()
        errors = []

        def feed():
            try:
                player.feed(speech, rate, 0)
                player.feed(
                    np.concatenate([np.zeros(rate * 3, dtype=np.float32), tone]),
                    rate,
                    0,
                    pause_frames=rate * 3,
                )
                player.drain(0)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=feed)
        thread.start()
        output = []
        try:
            wait_until(lambda: bool(recorder.streams) or done.is_set())
            deadline = time.monotonic() + 5
            while not done.is_set() and time.monotonic() < deadline:
                if recorder.streams and player._ring.buffered():
                    output.append(
                        recorder.streams[0].callback_once(player._ring.buffered())
                    )
                time.sleep(0.001)
            assert done.is_set()
            assert errors == []
            assert player.position(0) == pytest.approx(10, abs=1 / rate)
            return np.concatenate(output)
        finally:
            player.stop(1)
            thread.join(timeout=2)
            player.close()

    normal, fast = render(3), render(4)
    assert (len(normal) - len(fast)) / device_rate == pytest.approx(0.75, abs=0.05)
    np.testing.assert_array_equal(normal[:device_rate], fast[:device_rate])
    assert np.max(np.abs(np.diff(fast))) < 0.15


class FakeStream:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def callback_once(self, frames: int = BLOCKSIZE) -> np.ndarray:
        output = np.empty((frames, 1), dtype=np.float32)
        self.kwargs["callback"](
            output,
            frames,
            None,
            SimpleNamespace(output_underflow=False),
        )
        return output[:, 0]


class Recorder:
    """A stream factory that keeps every stream it opened, and a device
    query whose answer the test can change mid-Narration."""

    def __init__(self, device: OutputDevice = BUILT_IN) -> None:
        self.device = device
        self.streams: list[FakeStream] = []
        self.queries = 0

    def query(self) -> OutputDevice:
        self.queries += 1
        return self.device

    def open(self, **kwargs) -> FakeStream:
        stream = FakeStream(**kwargs)
        self.streams.append(stream)
        return stream

    def player(self, **kwargs) -> SoundDevicePlayback:
        return SoundDevicePlayback(
            query_device=self.query, stream_factory=self.open, **kwargs
        )


def test_resample_poly_converts_to_device_rate_without_moving_duration():
    source_rate = 24_000
    target_rate = 48_000
    seconds = 0.1
    source = np.sin(
        2 * np.pi * 1_000 * np.arange(int(source_rate * seconds)) / source_rate
    ).astype(np.float32)

    converted = resample_poly(source, source_rate, target_rate)

    assert len(converted) == int(target_rate * seconds)
    assert np.sqrt(np.mean(converted**2)) == pytest.approx(
        np.sqrt(np.mean(source**2)), rel=0.01
    )
    crossings = np.flatnonzero(np.diff(np.signbit(converted)))
    assert len(crossings) == pytest.approx(200, abs=2)


def test_resample_poly_is_a_no_op_at_the_device_rate():
    source = np.arange(16, dtype=np.float32)

    converted = resample_poly(source, 48_000, 48_000)

    assert converted is source


def test_ring_buffer_butt_joins_segments_and_zero_fills_the_remainder():
    ring = PcmRingBuffer()
    ring.push(TimedPcm(np.array([1, 2], dtype=np.float32), 2))
    ring.push(TimedPcm(np.array([3], dtype=np.float32), 1))

    output, filled, source_frames = ring.read(5)

    assert filled == 3
    assert source_frames == 3
    np.testing.assert_array_equal(output, [1, 2, 3, 0, 0])
    assert ring.empty()


def test_ring_buffer_reports_room_against_its_capacity():
    ring = PcmRingBuffer(capacity=4)

    assert ring.has_room()
    ring.push(frames(3))
    assert ring.buffered() == 3
    assert ring.has_room()

    ring.push(frames(2))
    assert ring.buffered() == 5
    assert not ring.has_room()

    ring.read(5)
    assert ring.buffered() == 0
    assert ring.has_room()


def test_an_uncapped_ring_always_has_room():
    ring = PcmRingBuffer()
    ring.push(frames(10_000))

    assert ring.has_room()


def test_playback_opens_one_native_rate_stream_with_ear_calibrated_settings():
    recorder = Recorder()
    player = recorder.player()
    played: list[float] = []
    thread = threading.Thread(
        target=lambda: played.append(
            player.play(np.ones(2_400, dtype=np.float32), 24_000, 0)
        )
    )
    thread.start()
    wait_until(lambda: len(recorder.streams) == 1)
    wait_until(lambda: not player._ring.empty())
    chunks = []
    deadline = time.monotonic() + 2
    while thread.is_alive() and time.monotonic() < deadline:
        chunks.append(recorder.streams[0].callback_once())
        thread.join(timeout=0.005)
    output = np.concatenate(chunks)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert recorder.streams[0].started
    assert recorder.streams[0].kwargs | {"callback": None} == {
        "samplerate": 48_000,
        "channels": 1,
        "dtype": "float32",
        "callback": None,
        "latency": LATENCY_SECONDS,
        "blocksize": BLOCKSIZE,
    }
    assert np.count_nonzero(output) > 4_700
    assert played == pytest.approx([0.1])
    player.close()
    assert recorder.streams[0].closed


def test_callback_records_a_late_deadline_without_touching_the_audio():
    # Opening the stream reads the clock once, before either callback.
    times = iter([0.0, 10.0, 10.2])
    recorder = Recorder()
    player = recorder.player(clock=lambda: next(times))
    prefill(player, BLOCKSIZE * 2)

    first = recorder.streams[0].callback_once()
    second = recorder.streams[0].callback_once()

    np.testing.assert_array_equal(first, np.ones(BLOCKSIZE, dtype=np.float32))
    np.testing.assert_array_equal(second, np.ones(BLOCKSIZE, dtype=np.float32))
    # The first callback has no previous deadline to have missed.
    assert player.late_callbacks == 1
    player.close()


def test_feed_streams_without_waiting_and_drain_releases_the_resampler_tail():
    recorder = Recorder()
    player = recorder.player()
    chunk = np.ones(2_400, dtype=np.float32)
    fed = player.feed(chunk, 24_000, 0)
    fed += player.feed(chunk, 24_000, 0)

    # feed returned while audio is still queued — it streams, and 0.2s is
    # nowhere near the ring's ceiling.
    assert not player._ring.empty()

    drained: list[float] = []
    thread = threading.Thread(target=lambda: drained.append(player.drain(0)))
    thread.start()
    wait_until(lambda: drained or recorder.streams[0].callback_once() is None)
    thread.join(timeout=2)

    assert not thread.is_alive()
    # Chunk seams hold back a resampler tail; drain releases the remainder so
    # the narration's total duration is exact.
    assert fed + drained[0] == pytest.approx(0.2)
    player.close()


def test_feed_waits_once_the_ring_is_a_full_buffer_ahead_of_the_device():
    # Synthesis outruns realtime, so without a ceiling the ring grows to hold
    # the whole Narration — hundreds of MiB the next Stop throws away.
    recorder = Recorder()
    player = recorder.player()
    second = np.ones(2_400, dtype=np.float32)
    prefill(player, round(48_000 * MAX_BUFFERED_SECONDS))

    assert not player._ring.has_room()

    blocked: list[float] = []
    thread = threading.Thread(
        target=lambda: blocked.append(player.feed(second, 24_000, 0))
    )
    thread.start()
    time.sleep(0.1)
    assert not blocked

    # Draining the ring through the callback releases the waiting producer.
    while player._ring.buffered() > 0:
        recorder.streams[0].callback_once()
    thread.join(timeout=2)

    # One second of 24 kHz source, however many native frames that became.
    assert blocked[0] == pytest.approx(0.1, abs=0.01)
    player.close()


def test_a_stop_releases_a_feed_waiting_on_a_full_ring():
    recorder = Recorder()
    player = recorder.player()
    second = np.ones(2_400, dtype=np.float32)
    prefill(player, round(48_000 * MAX_BUFFERED_SECONDS))

    blocked: list[float] = []
    thread = threading.Thread(
        target=lambda: blocked.append(player.feed(second, 24_000, 0))
    )
    thread.start()
    time.sleep(0.05)

    player.stop(1)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert blocked == [0.0]
    player.close()


def test_a_late_stop_cannot_silence_the_narration_that_replaced_it():
    # The delayed-stop race: generation 1 is cancelled while its stop is
    # still in flight, generation 2 is already feeding the device, and 1's
    # stop finally lands. Playback is keyed by the worker's generation, so
    # the stale stop is a no-op instead of a silenced Narration.
    player = Recorder().player()
    player.stop(2)
    queued = player.feed(np.ones(2_400, dtype=np.float32), 24_000, 2)

    player.stop(1)

    assert queued > 0
    assert not player._ring.empty()
    assert player.feed(np.ones(2_400, dtype=np.float32), 24_000, 2) > 0
    # The cancelled generation is still refused, stale stop or not.
    assert player.feed(np.ones(2_400, dtype=np.float32), 24_000, 1) == 0.0
    player.close()


def test_stop_wakes_playback_waiting_for_a_callback():
    recorder = Recorder()
    player = recorder.player()
    thread = threading.Thread(
        target=lambda: player.play(np.ones(24_000, dtype=np.float32), 24_000, 0)
    )
    thread.start()
    wait_until(lambda: len(recorder.streams) == 1)

    player.stop(1)
    thread.join(timeout=2)

    assert not thread.is_alive()
    player.close()


def test_a_finished_narration_leaves_a_stream_open_on_the_current_default():
    # Switching to AirPods mid-session must not leave every later Narration
    # coming out of the speaker the Engine happened to open at launch. The
    # follow runs at the idle moment after a Narration drains — off the
    # next Narration's time-to-first-audio — so the next one finds the
    # fresh stream already open and reuses it.
    recorder = Recorder()
    player = recorder.player()
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)
    recorder.device = OutputDevice(key="airpods", sample_rate=44_100)

    drained: list[float] = []
    thread = threading.Thread(target=lambda: drained.append(player.drain(0)))
    thread.start()
    wait_until(lambda: drained or recorder.streams[0].callback_once() is None)
    thread.join(timeout=2)

    assert len(recorder.streams) == 2
    assert recorder.streams[0].closed
    assert recorder.streams[1].kwargs["samplerate"] == 44_100

    player.stop(1)
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 1)
    assert len(recorder.streams) == 2
    player.close()


def test_chunks_of_one_narration_do_not_reopen_the_stream():
    recorder = Recorder()
    player = recorder.player()
    for _ in range(3):
        player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)
        recorder.streams[0].callback_once()

    assert len(recorder.streams) == 1
    player.close()


def test_a_device_that_stops_asking_for_samples_is_reopened():
    # PortAudio reports nothing when an output disappears — the callback just
    # stops — so drain would otherwise park the generation thread forever.
    now = [0.0]
    recorder = Recorder()
    player = recorder.player(clock=lambda: now[0])
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)

    drained: list[float] = []
    thread = threading.Thread(target=lambda: drained.append(player.drain(0)))
    thread.start()
    wait_until(lambda: len(recorder.streams) == 1)
    now[0] = STALL_SECONDS + 1
    wait_until(lambda: len(recorder.streams) == 2)

    assert recorder.streams[0].closed
    # The reopened stream drains what the dead one never took.
    while not player._ring.empty():
        recorder.streams[1].callback_once()
    thread.join(timeout=2)

    assert not thread.is_alive()
    player.close()


def test_an_output_that_never_comes_back_is_written_off_instead_of_hanging():
    now = [0.0]
    recorder = Recorder()
    player = recorder.player(clock=lambda: now[0])
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)

    outcomes: list[float | Exception] = []

    def drain() -> None:
        try:
            outcomes.append(player.drain(0))
        except Exception as error:
            outcomes.append(error)

    thread = threading.Thread(target=drain)
    thread.start()

    # Every reopened stream is just as dead as the last one.
    for step in range(1, 6):
        now[0] = step * (STALL_SECONDS + 1)
        time.sleep(0.05)
    thread.join(timeout=2)

    assert not thread.is_alive(), "drain must not park the generation thread"
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], AudioOutputUnavailable)
    assert player._ring.empty()
    # Bounded: the first stream, the recoveries the budget allows, then the
    # write-off — plus the one fresh stream drain's idle refresh opens for
    # whatever Narration comes next.
    assert len(recorder.streams) == 1 + MAX_STALL_RECOVERIES + 1
    player.close()


def test_a_written_off_output_raises_and_refreshes_for_the_next_narration():
    # Device loss is an outcome, not polling state: the operation tells its
    # caller immediately and refreshes at the idle boundary it created.
    now = [0.0]
    recorder = Recorder()
    player = recorder.player(clock=lambda: now[0])
    second = np.ones(2_400, dtype=np.float32)
    prefill(player, round(48_000 * MAX_BUFFERED_SECONDS))

    outcomes: list[float | Exception] = []

    def feed() -> None:
        try:
            outcomes.append(player.feed(second, 24_000, 0))
        except Exception as error:
            outcomes.append(error)

    thread = threading.Thread(target=feed)
    thread.start()
    for step in range(1, 6):
        now[0] = step * (STALL_SECONDS + 1)
        time.sleep(0.05)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], AudioOutputUnavailable)
    streams_after_write_off = len(recorder.streams)

    player.stop(1)
    assert player.feed(second, 24_000, 1) > 0
    assert len(recorder.streams) == streams_after_write_off
    player.close()


def test_stale_write_off_cannot_poison_the_replacement_generation():
    player = Recorder().player()
    player.stop(1)

    # Stage the last step of generation 0's stall recovery after generation
    # 1 has already replaced it.
    player._write_off(0)

    assert player.feed(np.ones(2_400, dtype=np.float32), 24_000, 1) > 0
    player.close()


def test_a_chunk_resampled_before_a_recovery_is_redone_at_the_new_rate():
    # A stall recovery inside feed's wait can land on a device at a new
    # rate; the chunk resampled for the old rate must not be pushed as-is —
    # it would play pitch-shifted and misreport its duration.
    now = [0.0]
    recorder = Recorder()
    player = recorder.player(clock=lambda: now[0])
    second = np.ones(2_400, dtype=np.float32)
    prefill(player, round(48_000 * MAX_BUFFERED_SECONDS))
    assert not player._ring.has_room()

    fed: list[float] = []
    thread = threading.Thread(target=lambda: fed.append(player.feed(second, 24_000, 0)))
    thread.start()
    time.sleep(0.05)
    recorder.device = OutputDevice(key="airpods", sample_rate=44_100)
    now[0] = STALL_SECONDS + 1
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert recorder.streams[-1].kwargs["samplerate"] == 44_100
    # One second of source audio, measured against the rate it will
    # actually play at.
    assert fed == [pytest.approx(0.1, abs=0.01)]
    assert player._ring.buffered() == pytest.approx(4_410, abs=20)
    player.close()


def test_a_nearly_full_ring_never_splits_a_processed_chunk_across_recovery():
    now = [0.0]
    recorder = Recorder()
    player = recorder.player(clock=lambda: now[0])
    player._ensure_stream()
    capacity = player._ring.capacity
    assert capacity is not None
    prefill(player, capacity - 1)

    # The first room check succeeds by one frame. A processed chunk must stay
    # atomic even when the default device has changed and the old stream is
    # stale; otherwise its remainder is queued at the new stream's sample rate.
    recorder.device = OutputDevice(key="airpods", sample_rate=44_100)
    now[0] = STALL_SECONDS + 1
    player.feed(np.ones(round(48_000 * PROCESS_SECONDS), dtype=np.float32), 48_000, 0)

    assert len(recorder.streams) == 1
    assert player._ring.buffered() <= capacity + round(2 * 48_000 * PROCESS_SECONDS)
    player.close()


def test_position_counts_only_frames_consumed_by_the_callback():
    recorder = Recorder()
    player = recorder.player()
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)
    assert player.position(0) == 0.0

    recorder.streams[0].callback_once(frames=2_400)
    assert player.position(0) == pytest.approx(0.05)
    player.close()


def test_level_follows_the_loudness_of_what_the_device_heard():
    recorder = Recorder()
    player = recorder.player()
    # 24 kHz in, 48 kHz out: every fed frame is two device frames.
    loud = np.full(2_400, 0.1, dtype=np.float32)  # -20 dBFS
    player.feed(np.concatenate([loud, np.zeros(2_400, dtype=np.float32)]), 24_000, 0)
    assert player.level(0) == 0.0

    recorder.streams[0].callback_once(frames=4_800)
    assert player.level(0) == pytest.approx(0.6, abs=0.02)
    assert player.level(1) == 0.0

    # The silent half, bar the resampler's tail of the loud one.
    recorder.streams[0].callback_once(frames=4_800)
    assert player.level(0) < 0.2

    # A paused device is silent, whatever is queued behind it.
    player.feed(loud, 24_000, 0)
    recorder.streams[0].callback_once(frames=4_800)
    assert player.level(0) > 0.5
    player.pause(0)
    recorder.streams[0].callback_once(frames=4_800)
    assert player.level(0) == 0.0
    player.close()


def test_starved_frames_count_silence_the_device_heard_mid_narration(caplog):
    recorder = Recorder()
    player = recorder.player()
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)

    # A callback the feeder could not keep up with plays silence past the
    # audio it had; that gap is starvation, unlike a pause.
    player.pause(0)
    recorder.streams[0].callback_once(frames=BLOCKSIZE)
    assert player.starved_frames == 0
    assert player.ring_starvations == 0
    player.unpause(0)

    buffered = player._ring.buffered()
    recorder.streams[0].callback_once(frames=buffered + 100)
    assert player.starved_frames == 100
    assert player.ring_starvations == 1
    assert player.underflows == 0

    # Once a drain has released everything, the trailing silence is the
    # natural end of the Narration, not a feeder that fell behind.
    thread = threading.Thread(target=lambda: player.drain(0))
    thread.start()
    while thread.is_alive():
        recorder.streams[0].callback_once()
    thread.join(timeout=2)
    assert player.starved_frames == 100
    assert player.ring_starvations == 1
    assert player.underflows == 0
    assert "ran dry for 2 ms" in caplog.text
    player.close()


def test_a_stopped_narration_reports_its_own_starvation(caplog):
    recorder = Recorder()
    player = recorder.player()
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)
    recorder.streams[0].callback_once(frames=player._ring.buffered() + 48)

    player.stop(1)
    assert "ran dry for 1 ms" in caplog.text
    caplog.clear()

    # The next Narration starts with a clean slate.
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 1)
    recorder.streams[0].callback_once(frames=2_400)
    thread = threading.Thread(target=lambda: player.drain(1))
    thread.start()
    while thread.is_alive():
        recorder.streams[0].callback_once()
    assert "ran dry" not in caplog.text
    player.close()


def test_pause_emits_silence_without_consuming_the_ring():
    recorder = Recorder()
    player = recorder.player()
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)
    buffered = player._ring.buffered()

    player.pause(0)
    silence = recorder.streams[0].callback_once(frames=512)

    # The device keeps calling back — which is what keeps a pause
    # distinguishable from a stall — but hears silence, and the playhead
    # does not move.
    assert np.all(silence == 0)
    assert player._ring.buffered() == buffered
    assert player.position(0) == 0.0

    player.unpause(0)
    recorder.streams[0].callback_once(frames=2_400)
    assert player.position(0) == pytest.approx(0.05)
    player.close()


def test_a_stop_clears_a_pause_left_by_the_previous_generation():
    recorder = Recorder()
    player = recorder.player()
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)
    player.pause(0)

    player.stop(1)
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 1)
    recorder.streams[0].callback_once(frames=2_400)

    assert player.position(1) == pytest.approx(0.05)
    player.close()


def test_a_pause_for_a_stale_generation_is_ignored():
    player = Recorder().player()
    player.stop(2)

    player.pause(1)

    assert player._paused is False
    player.close()


def test_new_generation_resets_position_and_old_generation_cannot_read_it():
    recorder = Recorder()
    player = recorder.player()
    player.feed(np.ones(2_400, dtype=np.float32), 24_000, 0)
    recorder.streams[0].callback_once(frames=2_400)
    player.stop(1)

    assert player.position(0) == 0.0
    assert player.position(1) == 0.0
    player.close()
