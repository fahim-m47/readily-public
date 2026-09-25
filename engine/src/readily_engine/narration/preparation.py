"""Ready Blocks handed from serial synthesis to the playback feeder."""

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

from readily_engine.storage.storage import PlannedSegment


class Cancelled(Exception):
    """The Narration was replaced, stopped, or lost its feeder."""


@dataclass(frozen=True)
class ReadySegment:
    segment: PlannedSegment
    gap: bool


class Preparation:
    """Queue only cache references; a full playback ring cannot block publication.

    The scheduler owns publication, the feeder owns consumption, and either
    can abort the run. The generation counter remains the cancellation authority.
    """

    def __init__(self, current: Callable[[], bool]) -> None:
        self._current = current
        self._ready: queue.Queue[ReadySegment] = queue.Queue()
        self._aborted = threading.Event()
        self._finished = threading.Event()

    def abort(self) -> None:
        """Cancel the run from whichever side gave up on it."""
        self._aborted.set()

    def finish(self) -> None:
        """Report that the feeder is done with this Preparation."""
        self._finished.set()

    def wait_finished(self, timeout: float) -> bool:
        """Wait for the feeder to finish; False once `timeout` passes."""
        return self._finished.wait(timeout=timeout)

    def cancelled(self) -> bool:
        return self._aborted.is_set() or not self._current()

    def publish(self, segment: PlannedSegment, *, gap: bool) -> None:
        self._ready.put(ReadySegment(segment, gap))

    def take(self) -> ReadySegment:
        """Block for the next published Block, or raise once cancelled."""
        while not self.cancelled():
            try:
                return self._ready.get(timeout=0.05)
            except queue.Empty:
                pass
        raise Cancelled
