"""Chunked resampling must be indistinguishable from whole-signal resampling.

A per-chunk `resample_poly` assumes silence past both chunk edges and mints a
seam transient every chunk — the artifact class a listening pass isolated.
The streaming resampler's contract: concatenating its emissions reproduces
the whole-signal result to within float32 rounding
(BLAS reorders reductions with matrix shape, so 1-ULP noise is expected; a
real seam transient measures orders of magnitude above this bound).
"""

import numpy as np
import pytest

from readily_engine.audio.resampling import StreamingResampler, resample_poly


def stream(pcm, chunk_sizes, source_rate, target_rate):
    resampler = StreamingResampler(source_rate, target_rate)
    pieces = []
    start = 0
    for size in chunk_sizes:
        pieces.append(resampler.feed(pcm[start : start + size]))
        start += size
    assert start == len(pcm)
    pieces.append(resampler.flush())
    return np.concatenate(pieces), resampler


@pytest.mark.parametrize(
    ("source_rate", "target_rate"),
    [
        (24_000, 48_000),  # qwen3/Kokoro to a 48k device
        (24_000, 44_100),  # rational ratio to a 44.1k device
        (22_050, 48_000),  # rational upsample the other way around
        (48_000, 24_000),  # downsample, for symmetry
    ],
)
def test_chunked_output_matches_whole_signal_output(source_rate, target_rate):
    rng = np.random.default_rng(26)
    pcm = rng.standard_normal(20_000).astype(np.float32)
    chunk_sizes = [1, 4_095, 7, 9_000, 0, 6_000, 897]

    ours, _ = stream(pcm, chunk_sizes, source_rate, target_rate)

    reference = resample_poly(pcm, source_rate, target_rate)
    assert ours.shape == reference.shape
    assert float(np.max(np.abs(ours - reference))) < 1e-6


def test_random_chunkings_stay_within_rounding_of_the_reference():
    rng = np.random.default_rng(7)
    pcm = rng.standard_normal(30_000).astype(np.float32)
    reference = resample_poly(pcm, 24_000, 48_000)
    for _ in range(5):
        sizes = []
        remaining = len(pcm)
        while remaining:
            size = int(rng.integers(1, 5_000))
            size = min(size, remaining)
            sizes.append(size)
            remaining -= size

        ours, _ = stream(pcm, sizes, 24_000, 48_000)

        assert ours.shape == reference.shape
        assert float(np.max(np.abs(ours - reference))) < 1e-6


def test_history_stays_bounded_over_a_long_stream():
    rng = np.random.default_rng(3)
    resampler = StreamingResampler(24_000, 44_100)
    for _ in range(200):
        resampler.feed(rng.standard_normal(2_048).astype(np.float32))
    assert len(resampler._history) < 20_000


def test_same_rate_is_a_passthrough():
    resampler = StreamingResampler(48_000, 48_000)
    pcm = np.arange(16, dtype=np.float32)
    np.testing.assert_array_equal(resampler.feed(pcm), pcm)
    assert len(resampler.flush()) == 0


def test_flush_resets_for_the_next_stream():
    pcm = np.sin(np.linspace(0, 40, 9_000)).astype(np.float32)
    resampler = StreamingResampler(24_000, 48_000)
    reference = resample_poly(pcm, 24_000, 48_000)
    for _ in range(2):
        first = resampler.feed(pcm[:5_000])
        rest = resampler.feed(pcm[5_000:])
        tail = resampler.flush()
        joined = np.concatenate([first, rest, tail])
        assert joined.shape == reference.shape
        assert float(np.max(np.abs(joined - reference))) < 1e-6
