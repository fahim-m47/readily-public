"""The in-engine resampler is the ear-calibrated scipy resample_poly, re-made.

The static debugging and listening runs were done with
scipy.signal.resample_poly; ADR 0006 keeps SciPy itself out of the shipped
Engine. This pins the replacement to the reference it must sound like —
against dev-only SciPy, which the production license gate never sees.
"""

from math import gcd

import numpy as np
import pytest

from readily_engine.audio.resampling import resample_poly

signal = pytest.importorskip("scipy.signal")


@pytest.mark.parametrize(
    ("source_rate", "target_rate"),
    [
        (24_000, 48_000),  # integer upsample: Kokoro to a 48k device
        (24_000, 44_100),  # rational upsample: Kokoro to a 44.1k device
        (24_000, 88_200),  # a hi-res device rate
        (48_000, 24_000),  # downsample, for symmetry
    ],
)
def test_matches_the_scipy_reference_tuned_by_ear(source_rate, target_rate):
    rng = np.random.default_rng(21)
    pcm = rng.standard_normal(4_800).astype(np.float32)

    ours = resample_poly(pcm, source_rate, target_rate)

    factor = gcd(source_rate, target_rate)
    reference = signal.resample_poly(
        pcm.astype(np.float64), target_rate // factor, source_rate // factor
    )
    assert ours.shape == reference.shape
    assert float(np.max(np.abs(ours - reference))) < 1e-3
