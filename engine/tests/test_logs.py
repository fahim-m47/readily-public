"""The Engine's stderr is the log file a reader sends; it never holds a Source."""

import io
import logging
import sys
import threading
from pathlib import Path

import pytest
from conftest import wait_until
from storage_fakes import open_test_storage
from worker_fakes import RecordingPlayback, RecordingSynthesizer, request, worker_for

from readily_engine.logs import MAX_LINE_CHARS, configure_logging

SOURCE = (
    "It was the best of times, it was the worst of times, it was the age of "
    "wisdom, it was the age of foolishness, it was the epoch of belief."
)


@pytest.fixture
def log():
    """The process's log as the shell would relay it, at DEBUG so every
    call site in the process is heard; the root logger put back after."""
    root = logging.getLogger()
    level = root.level
    hooks = sys.excepthook, threading.excepthook
    stream = io.StringIO()
    handler = configure_logging(stream, logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
        sys.excepthook, threading.excepthook = hooks


def test_a_line_is_level_logger_message(log):
    logging.getLogger("readily_engine.worker").warning("Block %d retried once", 4)

    assert log.getvalue() == "WARNING readily_engine.worker Block 4 retried once\n"


def test_a_line_the_length_of_prose_is_withheld_and_its_length_kept(log):
    # A call site that quotes what it choked on, at a length no status
    # line reaches.
    logging.getLogger("readily_engine.worker").warning(
        "could not read: %s", SOURCE * 20
    )

    written = log.getvalue()
    assert "best of times" not in written
    assert "-character line was withheld" in written
    assert all(len(line) <= MAX_LINE_CHARS for line in written.splitlines())


def test_a_path_under_the_home_directory_is_recorded_from_the_tilde(log):
    inside = Path.home() / "Library" / "Application Support" / "Readily" / "models"
    logging.getLogger("readily_engine.storage").warning("could not exclude %s", inside)

    written = log.getvalue()
    assert str(Path.home()) not in written, written
    assert written.endswith(
        "could not exclude ~/Library/Application Support/Readily/models\n"
    )


def test_an_exception_is_recorded_as_its_type_and_never_its_message(log):
    # A Block is shorter than the length rule, so an exception that quotes
    # one gets past it; the formatter drops the message instead.
    try:
        raise ValueError(SOURCE)
    except ValueError:
        logging.getLogger("readily_engine.worker").exception("Block 1 failed")

    written = log.getvalue()
    assert "best of times" not in written, written
    assert "ValueError: [the message was withheld" in written, written
    assert "Traceback (most recent call last):" in written
    assert "raise ValueError(SOURCE)" in written, written
    # The frame names this file, and not the checkout it sits in.
    assert 'File "test_logs.py", line' in written, written
    assert "/" not in written, written


class FailingOnceSynthesizer(RecordingSynthesizer):
    """Fails the first attempt at every Block, so the run takes the retry
    path: the one that logs a traceback from inside synthesis, with the
    Block's text in scope."""

    def __init__(self) -> None:
        super().__init__()
        self.failed: set[int] = set()

    def generate(self, record):
        if record.ordinal not in self.failed:
            self.failed.add(record.ordinal)
            raise RuntimeError("the model produced no audio")
        return super().generate(record)


def test_an_uncaught_exception_is_a_record_and_not_a_raw_traceback(log, capfd):
    def fail():
        raise ValueError(SOURCE)

    thread = threading.Thread(target=fail, name="worker-3")
    thread.start()
    thread.join()
    try:
        fail()
    except ValueError:
        sys.excepthook(*sys.exc_info())

    written = log.getvalue()
    assert "best of times" not in written, written
    assert written.count("ValueError: [the message was withheld") == 2, written
    assert "CRITICAL readily_engine uncaught exception in thread worker-3" in written
    assert "CRITICAL readily_engine uncaught exception\n" in written
    assert "best of times" not in capfd.readouterr().err


def test_a_whole_narration_run_logs_nothing_of_the_source(log, tmp_path, capfd):
    """Every logger in the process at DEBUG, through a full run: start,
    a failed and retried synthesis, playback, storage, finish, delete. The
    rule is what the call sites write, not only what the formatter
    withholds, so the sentence is short enough to pass the gate: if it
    shows up, a call site logged it."""
    storage = open_test_storage(tmp_path)
    worker = worker_for(FailingOnceSynthesizer(), RecordingPlayback(), storage)
    try:
        narration_id = worker.start(request(SOURCE))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        worker.delete(narration_id)
    finally:
        worker.close()
        storage.close()

    written = log.getvalue() + capfd.readouterr().err
    assert "Synthesis attempt 1 of Block" in written, written
    for words in ("best of times", "worst of times", "epoch of belief"):
        assert words not in written, written
