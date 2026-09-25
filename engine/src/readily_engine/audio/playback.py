"""Engine-owned callback playback at the output device's native sample rate."""

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import numpy.typing as npt

from readily_engine.audio import AudioOutputUnavailable, FloatPcm
from readily_engine.audio.pauses import SourceClock, pause_fraction, stretch_speed
from readily_engine.audio.resampling import StreamingResampler
from readily_engine.audio.stretch import TimedPcm, TimeStretch

logger = logging.getLogger(__name__)

LATENCY_SECONDS = 0.4

# Where a PCM chunk's RMS counts as silence for the reported `level`. Speech at
# a normal mastering level sits around -20 dBFS, which lands near 0.6.
LEVEL_FLOOR_DB = -50.0
BLOCKSIZE = 4096

# Keep processed audio short so a live speed change reaches the reader soon.
# Preparation has its own seconds budget and can keep filling the cache.
MAX_BUFFERED_SECONDS = 0.25
PROCESS_SECONDS = 0.025

# A stream whose callback stops firing while audio is queued is a dead
# output, not a slow one: PortAudio reports nothing when a device goes away.
# Comfortably above the configured latency, so a device that is merely slow
# to start is never torn down under a healthy Narration.
STALL_SECONDS = 2.0

# Consecutive stall recoveries before the output is written off. Write-off
# wakes the blocked operation, refreshes the idle output for what comes next,
# and raises a generation-scoped outcome so this Narration cannot be mistaken
# for one the listener heard to completion.
MAX_STALL_RECOVERIES = 2

# How long a producer parks between checks while it waits on the callback.
_WAIT_SECONDS = 0.05


def loudness(pcm: FloatPcm) -> float:
    """RMS of one PCM chunk on a 0-1 decibel scale, silence at `LEVEL_FLOOR_DB`.

    Cheap enough for the audio thread: one dot product, then scalar math."""
    rms = math.sqrt(float(pcm.dot(pcm)) / len(pcm)) if len(pcm) else 0.0
    if rms <= 0.0:
        return 0.0
    return min(1.0, max(0.0, 1.0 - 20.0 * math.log10(rms) / LEVEL_FLOOR_DB))


class OutputStream(Protocol):
    def start(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class OutputDevice:
    """The output a stream is open on.

    `key` names the device in logs and tests; it is never parsed, and the
    playback code never compares it — telling defaults apart needs the
    PortAudio re-initialization the query already pays for, so following
    the default means reopening, not comparing.
    """

    key: object
    sample_rate: int


class PcmRingBuffer:
    """A lock-protected FIFO of PCM chunks consumed only by the audio callback.

    Bounded when `capacity` is set: the callback must never block, so the
    producer waits for room before atomically pushing one processed chunk.
    The queue may exceed the target by that one small chunk.
    """

    def __init__(self, capacity: int | None = None) -> None:
        self._condition = threading.Condition()
        self._segments: deque[TimedPcm] = deque()
        self._offset = 0
        self._buffered = 0
        self.capacity = capacity

    def push(self, piece: TimedPcm) -> None:
        if not len(piece.pcm):
            return
        with self._condition:
            self._segments.append(piece)
            self._buffered += len(piece.pcm)
            self._condition.notify_all()

    def read(self, frames: int) -> tuple[FloatPcm, int, float]:
        output = np.zeros(frames, dtype=np.float32)
        filled = 0
        source_frames = 0.0
        with self._condition:
            while filled < frames and self._segments:
                segment = self._segments[0]
                count = min(frames - filled, len(segment.pcm) - self._offset)
                output[filled : filled + count] = segment.pcm[
                    self._offset : self._offset + count
                ]
                filled += count
                source_frames += segment.source_frames * count / len(segment.pcm)
                self._offset += count
                if self._offset == len(segment.pcm):
                    self._segments.popleft()
                    self._offset = 0
            self._buffered -= filled
            self._condition.notify_all()
        return output, filled, source_frames

    def empty(self) -> bool:
        with self._condition:
            return not self._segments

    def buffered(self) -> int:
        """Frames queued but not yet handed to the device."""
        with self._condition:
            return self._buffered

    def has_room(self) -> bool:
        """Whether the target queue can admit the next processed chunk."""
        with self._condition:
            return self.capacity is None or self._buffered < self.capacity

    def clear(self) -> None:
        with self._condition:
            self._segments.clear()
            self._offset = 0
            self._buffered = 0
            self._condition.notify_all()

    def wait_for_change(self, timeout: float) -> None:
        """Park until the ring is touched, or `timeout` elapses."""
        with self._condition:
            self._condition.wait(timeout)


def _default_query_device() -> OutputDevice:
    """The system's current default output.

    PortAudio builds its device list at initialization and does not rebuild
    it, so an output the user switched to after the Engine started is
    invisible until the list is torn down and remade. That is only legal
    with no stream open, which is why the caller queries between Narrations,
    with its own stream already closed.
    """
    import sounddevice as sd

    sd._terminate()
    sd._initialize()
    device = sd.query_devices(kind="output")
    return OutputDevice(
        key=(device.get("index"), device.get("name")),
        sample_rate=round(float(device["default_samplerate"])),
    )


def _default_stream_factory(**kwargs: object) -> OutputStream:
    import sounddevice as sd

    return sd.OutputStream(**kwargs)


class Pipeline:
    """One Narration's processing from its source rate to the device: a
    stream-long resampler, then the stretcher. Both keep state across
    Segment seams so seams carry no filter or pitch-period transients. A
    pipeline belongs to one generation at one source and device rate; the
    feeder replaces it when any of those change. Only the feeder thread
    touches it.
    """

    def __init__(self, generation: int, source_rate: int, device_rate: int) -> None:
        self.generation = generation
        self.source_rate = source_rate
        self._device_rate = device_rate
        self._resampler = StreamingResampler(source_rate, device_rate)
        self._stretch = TimeStretch(device_rate)
        self._source_clock = SourceClock()

    def matches(self, generation: int, source_rate: int, device_rate: int) -> bool:
        return (
            self.generation == generation
            and self.source_rate == source_rate
            and self._device_rate == device_rate
        )

    def feed(
        self, pcm: FloatPcm, speed: float, *, pause: bool = False
    ) -> tuple[TimedPcm, float]:
        """Process one chunk; return the piece and the device seconds it fed in."""
        original = len(pcm) * self._device_rate / self.source_rate
        if pause and pause_fraction(speed) < 1:
            pcm = pcm[: max(1, round(len(pcm) * pause_fraction(speed)))]
        self._source_clock.append(
            len(pcm) * self._device_rate / self.source_rate, original
        )
        native = self._resampler.feed(pcm)
        piece = self._stretch.feed(native, stretch_speed(speed))
        return TimedPcm(
            piece.pcm, self._source_clock.consume(piece.source_frames)
        ), len(native) / self._device_rate

    def flush(self, speed: float) -> tuple[TimedPcm, float]:
        """Release the resampler's held-back tail and the stretcher's remainder."""
        tail = self._resampler.flush()
        stretched = self._stretch.feed(tail, stretch_speed(speed))
        rest = self._stretch.flush()
        piece = TimedPcm(
            np.concatenate([stretched.pcm, rest.pcm]),
            self._source_clock.flush(),
        )
        return piece, len(tail) / self._device_rate

    def close(self) -> None:
        self._stretch.close()


class SoundDevicePlayback:
    """Own one native-rate PortAudio stream and feed it from a PCM ring buffer."""

    def __init__(
        self,
        query_device: Callable[[], OutputDevice] = _default_query_device,
        stream_factory: Callable[..., OutputStream] = _default_stream_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._query_device = query_device
        self._stream_factory = stream_factory
        self._clock = clock
        self._ring = PcmRingBuffer()
        self._stream: OutputStream | None = None
        self._sample_rate = 0
        # Set when recovery gives up, then consumed by the blocked playback
        # operation as an exception. Generation-scoped so a stale recovery
        # cannot poison the Narration that replaced it.
        self._lost_generation: int | None = None
        # `_epoch` is the generation whose audio the device may hear. The
        # caller owns the numbering (the generation worker's counter), so a
        # stop issued for an older generation cannot silence a newer one,
        # and `_epoch_lock` covers both the check and the ring write — the
        # two halves have to be one step or a stop can land between them.
        self._epoch_lock = threading.Lock()
        self._epoch = 0
        self._paused = False
        self._position_sec = 0.0
        # Loudness of the last PCM chunk handed to the device, 0-1 on a decibel
        # scale (`LEVEL_FLOOR_DB` is silence). Measured in the callback and
        # reported beside the playhead so the shell's orb can move with the
        # voice; `0.0` for silence, pauses and generations no longer heard.
        self._level = 0.0
        self._stream_opened_at = 0.0
        self._last_callback_at: float | None = None
        self._stalls = 0
        # Touched only by the one playback feeder thread.
        self._pipeline: Pipeline | None = None
        self._speed = 1.0
        self.late_callbacks = 0
        self.underflows = 0
        # Frames the callback filled with silence while a Narration still
        # had audio coming: the feeder fell behind the device. The tail a
        # drain waits out is not starvation, nor is a pause.
        self.starved_frames = 0
        self.ring_starvations = 0
        self._starved_reported = 0
        self._expecting_audio = False
        self._starving = False

    def play(self, pcm: FloatPcm, sample_rate: int, generation: int) -> float:
        """Play one complete utterance: a single feed, then drain."""
        return self.feed(pcm, sample_rate, generation) + self.drain(generation)

    def feed(
        self, pcm: FloatPcm, sample_rate: int, generation: int, *, pause_frames: int = 0
    ) -> float:
        """Queue one streamed chunk and return the seconds it enqueued.

        Chunks are resampled with stream-long state so chunk seams carry no
        filter-edge transients; the held-back tail comes out in `drain`.
        Playback starts as soon as the device wants samples. This blocks
        only once the ring is `MAX_BUFFERED_SECONDS` ahead of the device,
        and a Stop releases it immediately.
        """
        if self._was_stopped(generation):
            return 0.0
        self._raise_if_output_lost(generation)
        self._ensure_stream()
        # Cut before the pipeline so a long Segment never becomes a long
        # processed queue and a speed change can apply mid-Segment.
        step = max(1, round(sample_rate * PROCESS_SECONDS))
        seconds = 0.0
        offset = 0
        while offset < len(pcm):
            if not self._wait_for(self._ring.has_room, generation):
                return 0.0
            if self._pipeline is None or not self._pipeline.matches(
                generation, sample_rate, self._sample_rate
            ):
                self._close_pipeline()
                self._pipeline = Pipeline(generation, sample_rate, self._sample_rate)
            end = min(
                offset + step, pause_frames if offset < pause_frames else len(pcm)
            )
            piece, fed = self._pipeline.feed(
                pcm[offset:end], self._speed, pause=offset < pause_frames
            )
            offset = end
            if not self._enqueue(piece, generation):
                return 0.0
            self._expecting_audio = True
            seconds += fed
        return seconds

    def set_speed(self, speed: float) -> None:
        """Apply to the next small input chunk without discarding queued audio.

        A float assignment is atomic; the feeder reads it once per chunk."""
        self._speed = speed

    def _enqueue(self, piece: TimedPcm, generation: int) -> bool:
        # Processing and queueing are one sample-rate epoch. Waiting between
        # fragments would let recovery reopen at a new rate while `piece`
        # still contains samples prepared for the old stream.
        with self._epoch_lock:
            if generation != self._epoch:
                return False
            self._ring.push(piece)
            if len(piece.pcm):
                self._starving = False
        return True

    def drain(self, generation: int) -> float:
        """Release the pipeline's tail and block until the ring runs dry."""
        seconds = 0.0
        self._expecting_audio = False
        if (
            not self._output_lost(generation)
            and self._pipeline is not None
            and self._pipeline.generation == generation
        ):
            piece, seconds = self._pipeline.flush(self._speed)
            self._enqueue(piece, generation)
        self._close_pipeline()
        self._report_starvation()
        if not self._wait_for(self._ring.empty, generation):
            self._ring.clear()
        self._refresh_stream()
        return seconds

    def _close_pipeline(self) -> None:
        if self._pipeline is not None:
            self._pipeline.close()
            self._pipeline = None

    def _report_starvation(self) -> None:
        """Log what the Narration that just ended starved the device of.
        Called at every end: drain, Stop, and write-off."""
        starved = self.starved_frames - self._starved_reported
        if starved:
            self._starved_reported = self.starved_frames
            logger.warning(
                "Output ring ran dry for %d ms during the last Narration",
                round(1000 * starved / self._sample_rate),
            )

    def stop(self, generation: int) -> None:
        """Silence everything queued below `generation` and admit its audio.

        Monotone on purpose: a stop the caller issued for a generation the
        worker has already moved past is a no-op, so a delayed stop cannot
        throw away the Narration that replaced it.
        """
        with self._epoch_lock:
            if generation < self._epoch:
                return
            self._epoch = generation
            if self._lost_generation != generation:
                self._lost_generation = None
            self._paused = False
            self._position_sec = 0.0
            self._level = 0.0
            self._expecting_audio = False
            self._starving = False
            self._ring.clear()
        self._report_starvation()

    def pause(self, generation: int) -> None:
        """Freeze the playhead: the callback emits silence, consumes
        nothing, and the ring keeps whatever is queued. The device stays
        open — a paused stream still calls back, which is what keeps pause
        distinguishable from a stall."""
        with self._epoch_lock:
            if generation == self._epoch:
                self._paused = True
                self._level = 0.0

    def unpause(self, generation: int) -> None:
        """Let the callback consume the ring again, exactly where it left off."""
        with self._epoch_lock:
            if generation == self._epoch:
                self._paused = False

    def close(self) -> None:
        """Silence any generation and release the device. Terminal."""
        with self._epoch_lock:
            self._epoch += 1
            self._lost_generation = None
            self._paused = False
            self._position_sec = 0.0
            self._level = 0.0
            self._expecting_audio = False
            self._ring.clear()
        self._close_stream()
        self._close_pipeline()

    def _was_stopped(self, generation: int) -> bool:
        with self._epoch_lock:
            return generation != self._epoch

    def _wait_for(self, ready: Callable[[], bool], generation: int) -> bool:
        """Park the feeder until `ready()`, watching for a dead
        output on every tick. Returns False when a Stop came first.

        Cancellation and `ready` are evaluated outside the ring's condition
        lock: cancellation reaches into the epoch lock, and holding the ring's
        lock across it would invert the epoch-then-ring order `stop` relies
        on.
        """
        while True:
            if self._was_stopped(generation):
                return False
            self._raise_if_output_lost(generation)
            if ready():
                return True
            self._recover_if_stalled(generation)
            self._raise_if_output_lost(generation)
            self._ring.wait_for_change(_WAIT_SECONDS)

    def _recover_if_stalled(self, generation: int) -> None:
        """Reopen an output that has stopped asking for samples.

        PortAudio says nothing when a device disappears — the callback
        simply stops — so an unplugged output would otherwise leave every
        wait here parked forever, with the feeder behind it and
        every later Narration silent until the Engine restarts.
        """
        if self._stream is None:
            return
        since = (
            self._last_callback_at
            if self._last_callback_at is not None
            else self._stream_opened_at
        )
        if self._clock() - since < STALL_SECONDS:
            return
        self._stalls += 1
        if self._stalls > MAX_STALL_RECOVERIES:
            logger.error(
                "Output device is not accepting audio; dropping what is queued"
            )
            self._write_off(generation)
            return
        logger.warning("Output device stalled; reopening the stream")
        try:
            self._open_stream()
        except Exception:
            logger.exception("Reopening the output stream failed")
            self._write_off(generation)

    def _write_off(self, generation: int) -> None:
        """Give up on the output for the rest of this Narration: close the
        stream and discard the queue. The blocked operation refreshes the
        idle output and reports the loss to its caller. Waking every waiter
        through the cleared ring keeps it from parking forever.
        """
        with self._epoch_lock:
            if generation != self._epoch:
                return
            self._lost_generation = generation
            self._expecting_audio = False
            self._level = 0.0
        self._close_stream()
        self._ring.clear()
        self._report_starvation()

    def _output_lost(self, generation: int) -> bool:
        with self._epoch_lock:
            return generation == self._epoch and self._lost_generation == generation

    def _raise_if_output_lost(self, generation: int) -> None:
        """Refresh once, then turn device loss into a Narration outcome."""
        with self._epoch_lock:
            if generation != self._epoch or self._lost_generation != generation:
                return
            self._lost_generation = None
        self._refresh_stream()
        raise AudioOutputUnavailable

    def _ensure_stream(self) -> None:
        """Open the stream if the refresh failed and left none."""
        if self._stream is None:
            self._open_stream()

    def _refresh_stream(self) -> None:
        """Reopen on the system's current default output, between Narrations.

        This is the idle moment: the ring is empty and the generation
        thread — the stream's only owner — is here, so the PortAudio
        re-initialization the device query needs costs no time-to-first-
        audio; the next Narration finds the fresh stream already open. It
        also grants a fresh recovery budget — the stall counter bounds
        consecutive reopens no callback answered, and the user may well
        have plugged the output back in. A Narration started right after a
        Stop reuses whatever stream it finds; the follow catches up at the
        next completed one.
        """
        self._stalls = 0
        try:
            self._open_stream()
        except Exception:
            logger.exception("Refreshing the output stream failed")
            self._close_stream()

    def _open_stream(self) -> None:
        """(Re)open the output stream on the system's current default device."""
        self._close_stream()
        device = self._query_device()
        if self._sample_rate and device.sample_rate != self._sample_rate:
            self._ring.clear()
            self._close_pipeline()
        logger.info("Output stream open on %r at %d Hz", device.key, device.sample_rate)
        self._sample_rate = device.sample_rate
        self._ring.capacity = round(MAX_BUFFERED_SECONDS * self._sample_rate)
        self._stream_opened_at = self._clock()
        self._last_callback_at = None
        self._stream = self._stream_factory(
            samplerate=self._sample_rate,
            channels=1,
            dtype="float32",
            callback=self._callback,
            latency=LATENCY_SECONDS,
            blocksize=BLOCKSIZE,
        )
        self._stream.start()

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.close()
        except Exception:
            logger.exception("Closing the output stream failed")

    def _callback(
        self,
        outdata: npt.NDArray[np.float32],
        frames: int,
        _time_info: object,
        status: object,
    ) -> None:
        now = self._clock()
        expected = frames / self._sample_rate
        if (
            self._last_callback_at is not None
            and now - self._last_callback_at > 2 * expected
        ):
            self.late_callbacks += 1
        self._last_callback_at = now
        self._stalls = 0
        if getattr(status, "output_underflow", False):
            self.underflows += 1

        with self._epoch_lock:
            epoch = self._epoch
            if self._paused:
                outdata[:] = 0
                self._level = 0.0
                return
        # Held across the read so a Stop cannot clear the ring and reset the
        # position between this read and the credit below; otherwise the old
        # Narration's source frames would land on the new one's clock. The
        # read copies at most one block, short enough for the audio thread.
        with self._epoch_lock:
            if epoch != self._epoch:
                outdata[:] = 0
                self._level = 0.0
                return
            pcm, filled, source_frames = self._ring.read(frames)
            outdata[:, 0] = pcm
            self._position_sec += source_frames / self._sample_rate
            self._level = loudness(pcm)
            if self._expecting_audio:
                self.starved_frames += frames - filled
                self.ring_starvations += int(filled < frames)
            self._starving = (
                self._expecting_audio and filled < frames and self._position_sec > 0
            )

    def starving(self, generation: int) -> bool:
        """Whether the callback ran out of this Narration's audio and still waits.

        Ignore initial pipeline latency, pauses, and the final drain. New
        queued audio clears the signal without waiting for another callback.
        """
        with self._epoch_lock:
            return (
                generation == self._epoch
                and self._expecting_audio
                and not self._paused
                and self._starving
            )

    def position(self, generation: int) -> float:
        """Seconds of this generation actually handed to the output device."""
        with self._epoch_lock:
            if generation != self._epoch:
                return 0.0
            return self._position_sec

    def level(self, generation: int) -> float:
        """How loud the last PCM chunk handed to the device was, 0-1."""
        with self._epoch_lock:
            if generation != self._epoch:
                return 0.0
            return self._level
