"""Ear-calibrated regression tests for the Engine's PCM artifact fixes."""

from pathlib import Path

import numpy as np
import pytest

from readily_engine.audio.artifacts import (
    DRIFT_WINDOW_SECONDS,
    HF8K_THRESHOLD,
    SILENCE_RMS,
    declick,
    find_discontinuities,
    find_noise_bursts,
    frame_rms,
    hf8k_ratio,
    read_wav,
    remove_dc_drift,
    scrub_noise_bursts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "audio"


def test_declick_removes_the_captured_kokoro_impulses():
    sample_rate, raw = read_wav(
        FIXTURES / "qualification" / "039-kokoro_82m" / "seg02-raw.wav"
    )

    before = find_discontinuities(raw, sample_rate)
    cleaned, fixed = declick(raw, sample_rate)

    assert before.tolist() == [44871, 145046, 145047, 145050, 145051]
    assert fixed == 2
    assert find_discontinuities(cleaned, sample_rate).size == 0


def test_declick_leaves_sustained_high_diffs_untouched():
    sibilance = np.tile(np.array([-0.3, 0.3], dtype=np.float32), 240)

    cleaned, fixed = declick(sibilance, sample_rate=24_000)

    assert fixed == 0
    assert cleaned is sibilance


def test_scrubber_removes_the_captured_in_pause_crackle():
    sample_rate, raw = read_wav(
        FIXTURES / "qualification" / "024-qwen3-tts_0.6b" / "seg00-raw.wav"
    )

    before = find_noise_bursts(raw, sample_rate)
    cleaned, scrubbed = scrub_noise_bursts(raw, sample_rate)

    assert [(burst.start, burst.end) for burst in before] == [(180_720, 183_600)]
    assert scrubbed == 1
    assert find_noise_bursts(cleaned, sample_rate) == ()


def test_hf8k_scores_separate_the_captured_degenerate_generations():
    # The calibration the streaming accept gate in `loading/mlx_lane.py` rests
    # on: two captured static draws and the clean retry that followed them,
    # scored against the one threshold both lanes read.
    scores = [
        hf8k_ratio(read_wav(FIXTURES / "hf8k" / name)[1], 24_000)
        for name in ("016-degenerate.wav", "017-degenerate.wav", "018-clean.wav")
    ]

    assert scores == pytest.approx([0.2260, 0.0602, 0.0083], abs=0.0001)
    assert [score > HF8K_THRESHOLD for score in scores] == [True, True, False]


def test_frame_rms_is_the_one_measurement_silence_is_judged_by():
    quiet = np.full(480, SILENCE_RMS / 2, dtype=np.float32)
    loud = np.full(480, 0.5, dtype=np.float32)

    frames, rms = frame_rms(np.concatenate([quiet, loud]), frame_width=240)

    assert frames.shape == (4, 240)
    assert (rms > SILENCE_RMS).tolist() == [False, False, True, True]


def test_frame_rms_drops_a_tail_shorter_than_one_frame():
    frames, rms = frame_rms(np.ones(100, dtype=np.float32), frame_width=240)

    assert frames.shape == (0, 240)
    assert not len(rms)


def drifting(rate: int = 24_000) -> np.ndarray:
    """A second of speech-like tone on a bias that grows across the draw,
    after a second of the true silence such an export pauses at."""
    time = np.arange(rate, dtype=np.float32) / rate
    tone = 0.4 * np.sin(2 * np.pi * 200 * time)
    return np.concatenate(
        [np.zeros(rate, dtype=np.float32), tone + np.linspace(0.02, 0.06, rate)]
    ).astype(np.float32)


def test_drift_removal_follows_a_bias_that_moves_across_the_draw():
    rate = 24_000
    pcm = drifting(rate)

    cleaned, peak = remove_dc_drift(pcm, rate)

    early = cleaned[rate + rate // 4 : rate + rate // 2]
    late = cleaned[rate + 3 * rate // 4 :]
    assert abs(float(np.mean(early))) < 5e-3
    assert abs(float(np.mean(late))) < 5e-3
    assert peak == pytest.approx(0.06, abs=0.01)


def test_drift_removal_leaves_the_draws_own_silence_silent():
    # The bug a single mean over the whole draw has: it subtracts the speech's
    # bias from silence that never carried one, and every absolute measurement
    # of a pause downstream then reads that silence as sound. The whole second
    # is inspected, not a safe slice of it, because the running mean has one
    # region where it does disturb the pause and the point is to hold that
    # region to its stated size.
    rate = 24_000
    half_window = round(rate * DRIFT_WINDOW_SECONDS / 2)

    cleaned, _peak = remove_dc_drift(drifting(rate), rate)

    _frames, rms = frame_rms(cleaned[:rate], rate // 100)
    settled = rms[: (rate - half_window) // (rate // 100)]
    assert float(np.max(settled)) < SILENCE_RMS
    # The half-window before the onset is the exception the docstring names:
    # the window straddles the boundary, so some of the speech's bias is
    # averaged out into the pause. It has to stay under the bias that is
    # smearing — 0.02 where this draw's tone begins — or the running mean is
    # doing something other than following the drift.
    assert float(np.max(np.abs(cleaned[:rate]))) < 0.02


def test_drift_removal_keeps_a_voice_it_has_no_room_to_measure():
    # Shorter than the running window, so there is nothing to track: fall
    # back to the one mean, which is what the whole draw's bias then is.
    rate = 24_000
    pcm = np.full(rate // 100, 0.3, dtype=np.float32)

    cleaned, peak = remove_dc_drift(pcm, rate)

    assert np.allclose(cleaned, 0.0, atol=1e-6)
    assert peak == pytest.approx(0.3)
