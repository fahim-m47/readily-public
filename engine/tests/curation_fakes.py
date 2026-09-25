"""Doubles the curation tests share: synthetic Voice Model output, two
fake Backends built on it, and an encoder that stands in for afconvert.

Kept beside `worker_fakes` rather than in one test module because the
capture and curation suites drive the same fake Backend and would otherwise
each grow their own tone.
"""

from pathlib import Path

import numpy as np

from readily_engine.audio import FloatPcm
from readily_engine.generation import GeneratedAudio

SAMPLE_RATE = 24_000


def speech(
    seconds: float, *, amplitude: float = 0.5, dc: float = 0.0, noise: float = 0.0
) -> FloatPcm:
    """Synthetic Voice Model output: a 200Hz tone under a long fade, padded
    with the leading and trailing silence every real model emits."""
    samples = int(seconds * SAMPLE_RATE)
    time = np.arange(samples, dtype=np.float32) / SAMPLE_RATE
    envelope = np.minimum(1.0, np.minimum(time, seconds - time) / 0.3)
    tone = np.sin(2 * np.pi * 200 * time).astype(np.float32) * envelope * amplitude
    if noise:
        generator = np.random.default_rng(0)
        tone = tone + generator.normal(0, noise, samples).astype(np.float32)
    padding = np.zeros(int(0.1 * SAMPLE_RATE), dtype=np.float32)
    return np.concatenate([padding, tone + dc, padding]).astype(np.float32)


class FakeSynthesizer:
    """A Backend that narrates every Block with the same clean tone."""

    def __init__(self, **flaws: float) -> None:
        self.flaws = flaws
        self.spoken: list[tuple[str, str]] = []

    def generate(self, record):
        text = record.text
        voice = record.voice_id
        self.spoken.append((text, voice))
        return GeneratedAudio(
            speech(0.1 * len(text) / 10 + 1.0, **self.flaws), SAMPLE_RATE
        )


# One 10ms frame of speech — under the 15ms crossfade window, so the
# `Assembler` cannot blend it and plays the previous seam's tail out plain.
CLIPPED_SECONDS = 0.01


class ClippedSynthesizer(FakeSynthesizer):
    """A Backend that all but stops speaking on Blocks under `chars`.

    Real models do this on a short Block at the tail of a waterfall cut, and
    it is the one input that makes the `Assembler` emit a lead that is the
    *previous* Block's speech rather than an authored pause."""

    def __init__(self, chars: int, **flaws: float) -> None:
        super().__init__(**flaws)
        self.chars = chars

    def generate(self, record):
        text = record.text
        voice = record.voice_id
        if len(text.strip()) >= self.chars:
            return super().generate(record)
        self.spoken.append((text, voice))
        padding = np.zeros(int(0.1 * SAMPLE_RATE), dtype=np.float32)
        samples = int(CLIPPED_SECONDS * SAMPLE_RATE)
        time = np.arange(samples, dtype=np.float32) / SAMPLE_RATE
        tone = (np.sin(2 * np.pi * 200 * time) * 0.5).astype(np.float32)
        return GeneratedAudio(np.concatenate([padding, tone, padding]), SAMPLE_RATE)


def encode_stub(pcm: FloatPcm, sample_rate: int, m4a_path: Path) -> None:
    """Stand in for afconvert, which only exists on macOS."""
    m4a_path.write_bytes(f"{sample_rate}:{len(pcm)}".encode())
