"""Playback-only pause compression and its map back to the saved timeline."""

from collections import deque

from readily_engine.audio import MAX_STRETCH_SPEED


def stretch_speed(speed: float) -> float:
    return min(speed, MAX_STRETCH_SPEED)


def pause_fraction(speed: float) -> float:
    """Keep all authored silence through 3x, tapering to one quarter at 4x."""
    return 1 - 0.75 * max(0, speed - MAX_STRETCH_SPEED)


class SourceClock:
    """Map consumed compressed frames back to original frames at the device rate.

    Append before resampling; consume only when the stretcher emits audio.
    The queue covers the processing latency, so shortened pauses cannot credit
    time to speech that is still playing ahead of them.
    """

    def __init__(self) -> None:
        self._spans: deque[tuple[float, float]] = deque()

    def append(self, frames: float, original: float) -> None:
        if frames:
            self._spans.append((frames, original))

    def consume(self, frames: float) -> float:
        original = 0.0
        while frames > 0 and self._spans:
            length, source = self._spans.popleft()
            taken = min(frames, length)
            credit = source * taken / length
            original += credit
            frames -= taken
            if taken < length:
                self._spans.appendleft((length - taken, source - credit))
        return original

    def flush(self) -> float:
        original = sum(source for _, source in self._spans)
        self._spans.clear()
        return original
