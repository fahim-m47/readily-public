"""Real TDHS output and its source clock, without an audio device."""

import numpy as np
import pytest

from readily_engine.audio.stretch import TimeStretch


@pytest.mark.parametrize("speed", [0.5, 1.0, 1.5, 2.0, 3.0])
def test_qualified_speeds_preserve_pitch_and_account_for_the_source(speed):
    rate = 24_000
    pcm = (0.4 * np.sin(2 * np.pi * 200 * np.arange(rate * 4) / rate)).astype(
        np.float32
    )
    stretch = TimeStretch(rate)
    try:
        pieces = [stretch.feed(part, speed) for part in np.array_split(pcm, 160)]
        pieces.append(stretch.flush())
    finally:
        stretch.close()
    output = np.concatenate([piece.pcm for piece in pieces])
    assert len(output) / rate == pytest.approx(4 / speed, abs=0.05)
    assert sum(piece.source_frames for piece in pieces) == len(pcm)
    middle = output[rate // 4 : -rate // 4]
    frequencies = np.fft.rfftfreq(len(middle), 1 / rate)
    assert frequencies[np.argmax(abs(np.fft.rfft(middle)))] == pytest.approx(200, abs=2)


def test_speed_changes_and_segment_boundaries_do_not_create_clicks():
    rate = 24_000
    pcm = (0.4 * np.sin(2 * np.pi * 200 * np.arange(rate * 4) / rate)).astype(
        np.float32
    )
    stretch = TimeStretch(rate)
    try:
        pieces = [
            stretch.feed(part, speed)
            for part, speed in zip(
                np.array_split(pcm, 4), [1.0, 0.5, 3.0, 1.5], strict=True
            )
        ]
        pieces.append(stretch.flush())
    finally:
        stretch.close()

    output = np.concatenate([piece.pcm for piece in pieces])
    assert sum(piece.source_frames for piece in pieces) == len(pcm)
    assert np.max(np.abs(np.diff(output))) < 0.15
