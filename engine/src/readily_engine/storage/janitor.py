"""Process-lifetime scheduling for Segment retention."""

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

JANITOR_INTERVAL_SECONDS = 60 * 60
JANITOR_SHUTDOWN_GRACE_SECONDS = 15


class RetentionJanitor:
    """Run retention periodically until the Engine begins shutting down.

    The schedule lives here and the policy lives in `NarrationStorage`, so
    this knows only that it has something to call and when. The first sweep
    runs immediately on `start`, which is also the Engine's boot sweep:
    orphans a crashed launch left behind are collected as the Engine comes
    up rather than an interval later.
    """

    def __init__(
        self,
        run_retention: Callable[[], object],
        *,
        interval_seconds: float = JANITOR_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Retention interval must be positive")
        self._run_retention = run_retention
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start one daemon thread; repeated calls leave it running once."""
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="readily-retention",
        )
        thread.start()
        self._thread = thread

    def close(self) -> None:
        """Wake the scheduler and wait out its current sweep, but no longer.

        The thread is a daemon, so a sweep still blocked on the filesystem
        after the grace period costs a log line rather than a shutdown that
        never completes.
        """
        self._stop.set()
        if self._thread is None:
            return
        self._thread.join(timeout=JANITOR_SHUTDOWN_GRACE_SECONDS)
        if self._thread.is_alive():
            logger.warning("Segment retention did not stop in time")

    def _run(self) -> None:
        while True:
            try:
                self._run_retention()
            except Exception:
                logger.exception("Scheduled Segment retention failed")
            if self._stop.wait(self._interval_seconds):
                return
