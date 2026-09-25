"""NumPy polyphase resampling for keeping CoreAudio at its native rate."""

from math import gcd

import numpy as np

from readily_engine.audio import FloatPcm


def _lowpass(up: int, down: int) -> FloatPcm:
    """Match the well-known resample_poly default: 20 taps per max factor."""
    max_rate = max(up, down)
    half_length = 10 * max_rate
    positions = np.arange(2 * half_length + 1, dtype=np.float64) - half_length
    cutoff = 1 / max_rate
    taps = cutoff * np.sinc(cutoff * positions)
    taps *= np.kaiser(len(taps), 5.0)
    taps /= taps.sum()
    return np.asarray(taps * up, dtype=np.float32)


def _output_length(filter_length: int, input_length: int, up: int, down: int) -> int:
    if input_length == 0:
        return 0
    return ((input_length - 1) * up + filter_length - 1) // down + 1


def _upfirdn(taps: FloatPcm, pcm: FloatPcm, up: int, down: int) -> FloatPcm:
    """Apply an FIR to an upsampled signal without materializing inserted zeros."""
    output_length = _output_length(len(taps), len(pcm), up, down)
    output = np.zeros(output_length, dtype=np.float32)
    # For j = residue + k*up, (j*down) mod up is constant. Each residue
    # therefore uses one phase of the FIR and strided source windows.
    for residue in range(min(up, output_length)):
        output_indices = np.arange(residue, output_length, up)
        phase = (residue * down) % up
        phase_taps = taps[phase::up]
        source_end = (output_indices * down) // up
        source_indices = source_end[:, None] - np.arange(len(phase_taps))[None, :]
        valid = (source_indices >= 0) & (source_indices < len(pcm))
        samples = np.zeros(source_indices.shape, dtype=np.float32)
        samples[valid] = pcm[source_indices[valid]]
        output[output_indices] = samples @ phase_taps
    return output


def resample_poly(pcm: FloatPcm, source_rate: int, target_rate: int) -> FloatPcm:
    """Resample mono float PCM by a reduced rational polyphase FIR.

    This is the in-engine equivalent of ``scipy.signal.resample_poly``.
    SciPy itself is intentionally not shipped: ADR 0006 rejects the copyleft
    runtime libraries its macOS wheels may bundle.
    """
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    if pcm.ndim != 1:
        raise ValueError("playback PCM must be mono")
    if source_rate == target_rate or len(pcm) == 0:
        return pcm

    factor = gcd(source_rate, target_rate)
    up = target_rate // factor
    down = source_rate // factor
    taps = _lowpass(up, down)
    half_length = (len(taps) - 1) // 2
    pre_pad = down - half_length % down
    pre_remove = (half_length + pre_pad) // down
    output_length = (len(pcm) * up + down - 1) // down
    taps = np.pad(taps, (pre_pad, 0))
    while _output_length(len(taps), len(pcm), up, down) < pre_remove + output_length:
        taps = np.pad(taps, (0, 1))

    filtered = _upfirdn(taps, np.asarray(pcm, dtype=np.float32), up, down)
    return np.asarray(
        filtered[pre_remove : pre_remove + output_length], dtype=np.float32
    )


class StreamingResampler:
    """Chunk-at-a-time resampling, bit-identical to whole-signal `resample_poly`.

    Resampling each chunk independently assumes silence beyond both chunk
    edges, which mints a filter-edge transient at every seam — the exact
    artifact class a listening pass isolated. This keeps enough raw
    history that every emitted sample's filter window is fully settled, and
    holds back samples whose window still extends past the newest input;
    `flush` releases them when the stream ends.
    """

    def __init__(self, source_rate: int, target_rate: int) -> None:
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("sample rates must be positive")
        self._source_rate = source_rate
        self._target_rate = target_rate
        self._passthrough = source_rate == target_rate
        if self._passthrough:
            return
        factor = gcd(source_rate, target_rate)
        self._up = target_rate // factor
        self._down = source_rate // factor
        taps_length = 2 * 10 * max(self._up, self._down) + 1
        half_length = (taps_length - 1) // 2
        pre_pad = self._down - half_length % self._down
        self._pre_remove = (half_length + pre_pad) // self._down
        # Input samples one output sample draws on (zero-padded taps add none).
        self._support = -(-(taps_length + pre_pad) // self._up)
        # Raw left context retained after emitting, in whole `down` blocks so
        # the trimmed window's output offset stays an integer.
        keep = 4 * self._support + self._down
        self._keep = keep + (-keep) % self._down
        self._history = np.zeros(0, dtype=np.float32)
        self._offset = 0  # global index of history[0]; multiple of `down`
        self._emitted = 0  # global count of output samples already returned

    def feed(self, pcm: FloatPcm) -> FloatPcm:
        """Absorb a chunk and return every output sample now final."""
        if self._passthrough:
            return np.asarray(pcm, dtype=np.float32)
        if len(pcm):
            self._history = np.concatenate(
                [self._history, np.asarray(pcm, dtype=np.float32)]
            )
        emitted = self._emit(final=False)
        self._trim()
        return emitted

    def flush(self) -> FloatPcm:
        """Return the held-back tail and reset for the next stream."""
        if self._passthrough:
            return np.zeros(0, dtype=np.float32)
        emitted = self._emit(final=True)
        self._history = np.zeros(0, dtype=np.float32)
        self._offset = 0
        self._emitted = 0
        return emitted

    def _emit(self, final: bool) -> FloatPcm:
        window = resample_poly(self._history, self._source_rate, self._target_rate)
        offset_out = self._offset * self._up // self._down
        if final:
            high = len(window)
        else:
            # W[k] is final once its last drawn input sample already exists.
            last_final = (len(self._history) * self._up - 1) // self._down
            high = min(len(window), last_final - self._pre_remove + 1)
        low = self._emitted - offset_out
        if low < 0 or (not final and self._offset and self._first_settled() > low):
            raise RuntimeError("streaming resampler lost required history")
        if high <= low:
            return np.zeros(0, dtype=np.float32)
        self._emitted = offset_out + high
        return np.asarray(window[low:high], dtype=np.float32)

    def _first_settled(self) -> int:
        """First window output index whose filter never reaches before the
        window: below it, a trimmed window would differ from the full signal."""
        first = (self._support - 1) * self._up + self._down - 1
        return max(0, first // self._down - self._pre_remove)

    def _trim(self) -> None:
        excess = len(self._history) - self._keep
        consumed = (
            (self._emitted - self._offset * self._up // self._down)
            * (self._down)
            // self._up
        )
        trim = min(excess, consumed)
        trim -= trim % self._down
        if trim > 0:
            self._history = self._history[trim:]
            self._offset += trim
