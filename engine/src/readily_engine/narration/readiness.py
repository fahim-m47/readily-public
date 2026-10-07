"""Ready listening time and measured generation cost for one playback run."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal, TypedDict

from readily_engine.audio.pauses import pause_fraction, stretch_speed


class BlockDiagnostics(TypedDict):
    ordinal: int
    recordHash: str
    seed: int
    cacheHit: bool
    wordTiming: Literal["spoken", "matched", "estimated"]
    supportModel: str | None
    take: Literal["A", "B"]
    hasComparison: bool


class Progress(TypedDict):
    """What Readiness itself measures; the Engine adds the device counters."""

    audioSecondsPerSecond: float | None
    readySecondsAhead: float
    preparingBlock: int | None
    generationComplete: bool
    playingBlock: BlockDiagnostics | None
    retries: int
    cutoffs: int


class Readiness:
    """What the scheduler has prepared ahead of the playhead, and how fast.

    Positions remain in source seconds.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._end = 0.0
        self._pauses: list[tuple[float, float]] = []
        self._speech_seconds = 0.0
        self._pause_seconds = 0.0
        self._generation_seconds = 0.0
        self._started: float | None = None
        self._complete = False
        self._preparing: int | None = None
        self._blocks: list[tuple[float, float, BlockDiagnostics]] = []
        self._retries = 0
        self._cutoffs = 0

    def preparing(self, ordinal: int | None) -> None:
        with self._lock:
            self._preparing = ordinal

    def attempted(self, *, retry: bool = False, cutoffs: int = 0) -> None:
        with self._lock:
            self._retries += int(retry)
            self._cutoffs += cutoffs

    def block(self, start: float, end: float, block: BlockDiagnostics) -> None:
        with self._lock:
            self._blocks.append((start, end, block))

    def progress(self, position: float, speed: float) -> Progress:
        """Report speech seconds per second of finished synthesis calls."""
        with self._lock:
            playing = next(
                (
                    block
                    for start, end, block in reversed(self._blocks)
                    if start <= position <= end
                ),
                None,
            )
            return {
                "audioSecondsPerSecond": self._speech_seconds / self._generation_seconds
                if self._generation_seconds
                else None,
                "readySecondsAhead": self._seconds_ahead_locked(position, speed),
                "preparingBlock": self._preparing,
                "generationComplete": self._complete,
                "playingBlock": playing,
                "retries": self._retries,
                "cutoffs": self._cutoffs,
            }

    @contextmanager
    def generating(self) -> Iterator[None]:
        """Measure one synthesis call."""
        with self._lock:
            started = self._started = self._clock()
        try:
            yield
        finally:
            with self._lock:
                self._generation_seconds += self._clock() - started
                self._started = None

    def add(
        self, *, start: float, pause: float, seconds: float, generated: bool
    ) -> None:
        """Record one Segment: its authored lead pause and its speech length."""
        with self._lock:
            if pause:
                self._pauses.append((start - pause, start))
            if generated:
                self._pause_seconds += pause
                self._speech_seconds += seconds

    def advance(self, end: float) -> None:
        with self._lock:
            self._end = end

    def finish(self, end: float) -> None:
        with self._lock:
            self._end = end
            self._complete = True

    def seconds_ahead(self, position: float, speed: float) -> float:
        with self._lock:
            return self._seconds_ahead_locked(position, speed)

    def _seconds_ahead_locked(self, position: float, speed: float) -> float:
        pause = sum(max(0, end - max(start, position)) for start, end in self._pauses)
        remaining = self._end - position - pause * (1 - pause_fraction(speed))
        return max(0, remaining) / stretch_speed(speed)

    def behind(
        self, position: float, speed: float, budget: float, *, starving: bool
    ) -> bool:
        with self._lock:
            produced = (
                self._speech_seconds + self._pause_seconds * pause_fraction(speed)
            ) / stretch_speed(speed)
            ahead = self._seconds_ahead_locked(position, speed)
            slower_than_playback = (
                self._generation_seconds > 0 and produced < self._generation_seconds
            )
            exhausted = self._started is not None and starving
            return (
                not self._complete
                and ahead < budget
                and (slower_than_playback or exhausted)
            )
