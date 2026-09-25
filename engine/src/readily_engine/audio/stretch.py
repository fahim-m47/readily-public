"""Streaming TDHS with source-time accounting for the playback ring."""

from dataclasses import dataclass

import numpy as np
from audiostretchy.interface.tdhs import TDHSAudioStretch

from readily_engine.audio import MIN_PLAYBACK_SPEED, FloatPcm

# TDHS tracks pitch periods between these frequencies, which span speaking
# voices; longer periods would smear consonants, shorter ones miss low voices.
_HIGHEST_PITCH_HZ = 333
_LOWEST_PITCH_HZ = 55
# TDHS's ratio is output per input, so the slowest speed makes the most audio.
_MAX_RATIO = 1 / MIN_PLAYBACK_SPEED


@dataclass(frozen=True)
class TimedPcm:
    """Audio and the source frames it represents, at the same sample rate."""

    pcm: FloatPcm
    source_frames: float


class TimeStretch:
    """Keep one TDHS handle until seek or EOF, including across speed changes.

    TDHS holds input before returning audio. Credit only emitted audio to
    the source clock, at the speed applied to that call, bounded by input
    received. Flush credits the remainder: TDHS rounds to pitch periods and
    emits its last buffered samples at unity rate. Timing within a pitch
    period is approximate; buffering never advances the audible playhead.
    Only the feeder thread owns or calls this handle.
    """

    def __init__(self, sample_rate: int) -> None:
        flags = TDHSAudioStretch.STRETCH_DUAL_FLAG
        if sample_rate >= 32_000:
            flags |= TDHSAudioStretch.STRETCH_FAST_FLAG
        self._processor = TDHSAudioStretch(
            sample_rate // _HIGHEST_PITCH_HZ, sample_rate // _LOWEST_PITCH_HZ, 1, flags
        )
        if not self._processor.handle:
            raise MemoryError("AudioStretch allocation failed")
        self._pending_source = 0.0

    def feed(self, pcm: FloatPcm, speed: float) -> TimedPcm:
        samples = (np.clip(pcm, -1, 32767 / 32768) * 32768).astype(np.int16)
        output = np.empty(
            self._processor.output_capacity(len(samples), _MAX_RATIO), dtype=np.int16
        )
        count = self._processor.process_samples(
            samples, len(samples), output, 1 / speed
        )
        self._pending_source += len(samples)
        consumed = min(self._pending_source, count * speed)
        self._pending_source -= consumed
        return TimedPcm(output[:count].astype(np.float32) / 32768, consumed)

    def flush(self) -> TimedPcm:
        pieces = []
        while True:
            output = np.empty(
                self._processor.output_capacity(0, _MAX_RATIO), dtype=np.int16
            )
            count = self._processor.flush(output)
            if not count:
                break
            pieces.append(output[:count].astype(np.float32) / 32768)
        pcm = np.concatenate(pieces) if pieces else np.empty(0, dtype=np.float32)
        consumed, self._pending_source = self._pending_source, 0.0
        return TimedPcm(pcm, consumed)

    def close(self) -> None:
        if self._processor.handle:
            self._processor.deinit()
