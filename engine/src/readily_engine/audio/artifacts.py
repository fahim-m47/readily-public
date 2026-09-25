"""Ear-calibrated artifact detection and cleanup over mono float PCM."""

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt

from readily_engine.audio import FloatPcm

HF8K_THRESHOLD = 0.05

# The ear-calibrated floor for "this frame is a pause". Every part of the
# Engine that divides audio into silence and speech — the crackle scrubber
# here, the streaming runaway cutoff in `loading/mlx_lane.py` — measures against
# this one number, or they stop agreeing about what silence is.
SILENCE_RMS = 0.008


@dataclass(frozen=True)
class NoiseBurst:
    """One spectrally peaky, high-frequency event isolated inside a pause."""

    start: int
    end: int


def read_wav(path: str | Path) -> tuple[int, FloatPcm]:
    """Read a mono WAV into normalized float32 PCM without a media dependency."""

    return decode_wav(Path(path).read_bytes(), path)


def decode_wav(contents: bytes, path: str | Path) -> tuple[int, FloatPcm]:
    """Decode WAV bytes already in memory; `path` only names them in errors."""

    if len(contents) < 12 or contents[:4] != b"RIFF" or contents[8:12] != b"WAVE":
        raise ValueError(f"not a RIFF/WAVE file: {path}")

    audio_format = channels = sample_rate = bits_per_sample = None
    pcm_bytes = None
    offset = 12
    while offset + 8 <= len(contents):
        chunk_id, chunk_size = struct.unpack_from("<4sI", contents, offset)
        offset += 8
        chunk = contents[offset : offset + chunk_size]
        if len(chunk) != chunk_size:
            raise ValueError(f"truncated WAV chunk in {path}")
        if chunk_id == b"fmt ":
            if len(chunk) < 16:
                raise ValueError(f"invalid WAV format chunk in {path}")
            audio_format, channels, sample_rate, _, _, bits_per_sample = (
                struct.unpack_from("<HHIIHH", chunk)
            )
        elif chunk_id == b"data":
            pcm_bytes = chunk
        offset += chunk_size + (chunk_size % 2)

    if (
        None in (audio_format, channels, sample_rate, bits_per_sample)
        or pcm_bytes is None
    ):
        raise ValueError(f"WAV is missing format or audio data: {path}")
    if channels != 1:
        raise ValueError(f"qualification audio must be mono: {path}")

    if audio_format == 3 and bits_per_sample == 32:
        pcm = np.frombuffer(pcm_bytes, dtype="<f4")
    elif audio_format == 1 and bits_per_sample == 8:
        pcm = (np.frombuffer(pcm_bytes, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif audio_format == 1 and bits_per_sample == 16:
        pcm = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768
    elif audio_format == 1 and bits_per_sample == 32:
        pcm = np.frombuffer(pcm_bytes, dtype="<i4").astype(np.float32) / 2**31
    else:
        raise ValueError(
            f"unsupported WAV encoding format={audio_format} "
            f"bits={bits_per_sample}: {path}"
        )

    return sample_rate, np.asarray(pcm, dtype=np.float32)


def frame_rms(
    pcm: FloatPcm, frame_width: int
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
    """Split `pcm` into whole frames of `frame_width` samples and return the
    frames with their per-frame RMS — the one measurement `SILENCE_RMS` is
    calibrated against. A tail shorter than a frame is dropped, so both
    arrays come back empty for input shorter than one frame."""

    frame_count = len(pcm) // frame_width
    frames = pcm[: frame_count * frame_width].reshape(frame_count, frame_width)
    return frames, np.sqrt((frames**2).mean(axis=1))


def hf8k_ratio(pcm: FloatPcm, sample_rate: int) -> float:
    """Return the energy fraction above 8kHz, the calibrated static score."""

    if len(pcm) < 1024:
        return 0.0
    spectrum = np.abs(np.fft.rfft(pcm)) ** 2
    frequencies = np.fft.rfftfreq(len(pcm), 1 / sample_rate)
    total = float(spectrum.sum()) or 1.0
    return float(spectrum[frequencies > 8000].sum() / total)


def voiced_fraction(pcm: FloatPcm, sample_rate: int) -> float:
    """Fraction of active 30ms frames dominated by sub-1.5kHz energy — the
    voiced body every real utterance carries and broadband static lacks.

    Companion to `hf8k_ratio` for scoring short streamed chunks, where the
    whole-generation static threshold does not transfer: sibilance alone
    tops it (measured: clean first chunks up to 0.41 hf8k, all with a
    voiced fraction of at least 0.30, while broadband static sits near 0).
    """

    frame_width = int(sample_rate * 0.03)
    frames, rms = frame_rms(pcm, frame_width)
    if not len(frames):
        return 0.0
    active = frames[rms > SILENCE_RMS]
    if not len(active):
        return 0.0
    spectra = np.abs(np.fft.rfft(active, axis=1)) ** 2 + 1e-14
    frequencies = np.fft.rfftfreq(frame_width, 1 / sample_rate)
    low_share = spectra[:, frequencies < 1500].sum(axis=1) / spectra.sum(axis=1)
    return float((low_share > 0.5).mean())


# How much of a draw a running mean looks at when it measures the bias under
# the speech, and a trade in both directions with 100ms in the middle of it.
# 100ms is seven cycles at 70Hz, so a low male fundamental averages to
# nothing. Narrowing shortens the onset smear `remove_dc_drift` describes but
# makes it louder, because a short mean tracks the tone rather than the bias:
# at 20ms the residue peaks five times higher and comes out of the speech as
# well. The ratio is what matters here, because it holds at every amplitude;
# the absolute figures scale with the draw and are given in the docstring
# below. Widening leaves the smear quiet and long, and stops following a bias
# that moves. Both ends are pinned, in opposite tests: narrowing fails
# `test_drift_removal_leaves_the_draws_own_silence_silent` at 50ms, widening
# fails `test_drift_removal_follows_a_bias_that_moves_across_the_draw` at
# 400ms. The passing band is 80ms to 300ms.
DRIFT_WINDOW_SECONDS = 0.1


def remove_dc_drift(pcm: FloatPcm, sample_rate: int) -> tuple[FloatPcm, float]:
    """Take the sub-audible bias out from under speech, returning its peak.

    Some exports ride a positive offset while they voice and sit at true zero
    while they pause — Kitten TTS Nano drifts up to about +0.05 — and every
    downstream measurement of "silence" is absolute: `trim_silence` and the
    crackle scrubber both compare frame RMS against `SILENCE_RMS`, so biased
    speech has no silent frame in it, the pause the Assembler authors as true
    zeros meets it as a step the ear hears as a click, and the crackle sitting
    in the model's own pauses is never looked for. Subtracting one mean over
    the whole draw is what that description invites and is wrong twice: the
    bias is not constant, so the speech stays tilted, and the draw's real
    silence is pushed down to minus the mean, where it reads as sound. A
    running mean tracks the drift and leaves silence where it is — except
    for the half-window either side of a speech onset, where the window
    straddles the boundary and smears part of the bias out into the pause.
    That residue is confined to the half-window, 50ms. It is bounded by the
    speech rather than by the bias, because the window averages whatever it
    straddles, so it grows with amplitude and falls off with pitch: measured
    with no bias present at all, a 70Hz draw at the 0.4 amplitude the tests
    use smears to 0.018 and a full-scale one to about 0.045, while a 200Hz
    draw at 0.4 stays under `SILENCE_RMS` entirely. On a low voice it is
    above `SILENCE_RMS`, so those last few frames of a pause do read as
    sound, and both consequences of that are the harmless direction:
    `trim_silence` keeps up to 50ms more of the run-up instead of clipping
    the onset, and the crackle scrubber declines to touch the 50ms of pause
    nearest the speech, which is where a burst is masked anyway. On a high
    voice neither consequence arises, because the residue never crosses the
    line.
    """
    window = int(sample_rate * DRIFT_WINDOW_SECONDS) | 1
    if len(pcm) <= window:
        offset = float(np.mean(pcm)) if len(pcm) else 0.0
        return (pcm - np.float32(offset)).astype(np.float32), abs(offset)
    half = window // 2
    padded = np.pad(pcm.astype(np.float64), half, mode="edge")
    cumulative = np.cumsum(np.concatenate(([0.0], padded)))
    drift = (cumulative[window:] - cumulative[:-window]) / window
    return (pcm - drift.astype(np.float32)).astype(np.float32), float(
        np.max(np.abs(drift))
    )


def find_discontinuities(
    pcm: FloatPcm, sample_rate: int, threshold: float = 0.35
) -> npt.NDArray[np.int_]:
    """Find isolated sample steps while excluding sustained high-diff content."""

    diffs = np.abs(np.diff(pcm))
    neighborhood = int(0.005 * sample_rate)
    return np.array(
        [
            index
            for index in np.flatnonzero(diffs > threshold)
            if np.median(diffs[max(0, index - neighborhood) : index + neighborhood])
            < 0.05
        ],
        dtype=int,
    )


def declick(
    pcm: FloatPcm, sample_rate: int, threshold: float = 0.35
) -> tuple[FloatPcm, int]:
    """Replace isolated impulse-click clusters with an approximately 1ms ramp."""

    flags = find_discontinuities(pcm, sample_rate, threshold)
    if not len(flags):
        return pcm, 0

    padding = int(0.001 * sample_rate)
    clusters = [[int(flags[0]), int(flags[0])]]
    for index in flags[1:]:
        value = int(index)
        if value - clusters[-1][1] <= 2 * padding:
            clusters[-1][1] = value
        else:
            clusters.append([value, value])

    cleaned = pcm.copy()
    for low, high in clusters:
        start = max(0, low - padding)
        end = min(len(cleaned) - 1, high + 1 + padding)
        cleaned[start:end] = np.linspace(
            cleaned[start], cleaned[end], end - start, endpoint=False
        )
    return cleaned, len(clusters)


def find_noise_bursts(pcm: FloatPcm, sample_rate: int) -> tuple[NoiseBurst, ...]:
    """Find the ear-calibrated crackle signature inside silent pauses."""

    frame_width = sample_rate // 100
    _frames, rms = frame_rms(pcm, frame_width)
    frame_count = len(rms)
    if frame_count < 3:
        return ()

    active = rms > SILENCE_RMS
    runs: list[tuple[int, int]] = []
    index = 0
    while index < frame_count:
        if not active[index]:
            index += 1
            continue
        end = index
        while end < frame_count and active[end]:
            end += 1
        runs.append((index, end))
        index = end

    bursts: list[NoiseBurst] = []
    for run_index, (start_frame, end_frame) in enumerate(runs):
        previous_end = runs[run_index - 1][1] if run_index else 0
        next_start = (
            runs[run_index + 1][0] if run_index + 1 < len(runs) else frame_count
        )
        if start_frame - previous_end < 3 or next_start - end_frame < 3:
            continue

        start = start_frame * frame_width
        end = end_frame * frame_width
        event = pcm[start:end]
        spectrum = np.abs(np.fft.rfft(event)) ** 2 + 1e-14
        frequencies = np.fft.rfftfreq(len(event), 1 / sample_rate)
        total = float(spectrum.sum())
        if float(spectrum[frequencies >= 6000].sum() / total) < 0.10:
            continue
        if float(spectrum[frequencies < 1500].sum() / total) >= 0.10:
            continue
        high_band = spectrum[(frequencies >= 6000) & (frequencies < 12000)]
        if not len(high_band):
            continue
        flatness = float(np.exp(np.mean(np.log(high_band))) / high_band.mean())
        if flatness < 0.25:
            bursts.append(
                NoiseBurst(
                    start=start,
                    end=end,
                )
            )
    return tuple(bursts)


def scrub_noise_bursts(pcm: FloatPcm, sample_rate: int) -> tuple[FloatPcm, int]:
    """Mute isolated crackles with 3ms fades, iterating until none remain."""

    fade = int(0.003 * sample_rate)
    cleaned = pcm
    total = 0
    for _ in range(3):
        bursts = find_noise_bursts(cleaned, sample_rate)
        if not bursts:
            break
        cleaned = cleaned.copy()
        for burst in bursts:
            cleaned[burst.start : burst.end] = 0
            if burst.start >= fade:
                cleaned[burst.start - fade : burst.start] *= np.linspace(1, 0, fade)
            if burst.end + fade <= len(cleaned):
                cleaned[burst.end : burst.end + fade] *= np.linspace(0, 1, fade)
        total += len(bursts)
    return cleaned, total


def _first_nonzero(pcm: FloatPcm) -> int:
    """Index of the first audible sample, or `len(pcm)` for pure silence."""
    nonzero = np.flatnonzero(pcm != 0)
    return int(nonzero[0]) if len(nonzero) else len(pcm)


def compare_feed(
    tap: FloatPcm, expected: FloatPcm
) -> tuple[tuple[tuple[int, int], ...], int, int]:
    """Return inserted silence, corrupt samples, and expected samples not played."""

    tap_index = _first_nonzero(tap)
    expected_index = _first_nonzero(expected)
    inserted: list[tuple[int, int]] = []
    mismatches = 0
    while tap_index < len(tap) and expected_index < len(expected):
        if tap[tap_index] == expected[expected_index]:
            tap_index += 1
            expected_index += 1
            continue
        if tap[tap_index] == 0:
            run = 0
            while tap_index + run < len(tap) and tap[tap_index + run] == 0:
                run += 1
            if (
                tap_index + run < len(tap)
                and tap[tap_index + run] == expected[expected_index]
            ):
                inserted.append((tap_index, run))
                tap_index += run
                continue
        mismatches += 1
        tap_index += 1
        expected_index += 1
    return tuple(inserted), mismatches, len(expected) - expected_index
