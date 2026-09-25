"""The Engine schedules its own retention work instead of relying on macOS."""

import threading
import time

import pytest

from readily_engine.storage.janitor import RetentionJanitor


def test_janitor_sweeps_once_immediately_so_boot_collects_orphans():
    """Retention's first run is the boot sweep, not an interval from now.

    Nothing else collects what a crashed launch left on disk, so an Engine
    that only swept on the hour would carry those bytes for an hour.
    """
    swept = threading.Event()
    janitor = RetentionJanitor(swept.set, interval_seconds=3600)
    try:
        janitor.start()

        assert swept.wait(timeout=1)
    finally:
        janitor.close()


def test_janitor_keeps_running_after_one_retention_failure():
    completed = threading.Event()
    calls = 0

    def run_retention() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("disk temporarily unavailable")
        completed.set()

    janitor = RetentionJanitor(run_retention, interval_seconds=0.01)
    try:
        janitor.start()
        assert completed.wait(timeout=1)
        assert calls >= 2
    finally:
        janitor.close()


def test_close_stops_the_schedule_and_returns_even_mid_sweep(monkeypatch):
    """A sweep wedged on the filesystem must not wedge Engine shutdown.

    The thread is a daemon precisely so this is survivable; `close` waits
    out a normal sweep and then gives up rather than joining forever.
    """
    monkeypatch.setattr(
        "readily_engine.storage.janitor.JANITOR_SHUTDOWN_GRACE_SECONDS", 0.05
    )
    entered = threading.Event()
    release = threading.Event()

    def wedged_retention() -> None:
        entered.set()
        release.wait()

    janitor = RetentionJanitor(wedged_retention, interval_seconds=0.01)
    try:
        janitor.start()
        assert entered.wait(timeout=1)

        started = time.monotonic()
        janitor.close()
        elapsed = time.monotonic() - started

        # The sweep is still wedged, so an unbounded join would sit here
        # until `release` is set in the teardown below — which is to say,
        # forever, if the filesystem never answers.
        assert janitor._thread.is_alive()
        assert elapsed < 1
    finally:
        release.set()


def test_close_before_start_is_a_no_op():
    RetentionJanitor(lambda: None).close()


def test_a_non_positive_interval_is_rejected_rather_than_spun_on():
    with pytest.raises(ValueError):
        RetentionJanitor(lambda: None, interval_seconds=0)
