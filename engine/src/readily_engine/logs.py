"""How the Engine's records reach the log file a reader sends.

The shell relays the Engine's stderr into `<data folder>/logs/readily.log`
(shell `logs.rs`), the one file a reader is asked to attach to a bug report.
So what goes to stderr here is shaped the way that file is: one record per
line, `LEVEL logger message`, and never a Source. Every call site keeps to
ids, ordinals, counts and status; this module is the gate behind them, and
withholds any line long enough to be prose, records an exception without
its message, and spells the reader's home directory `~`. `readily.db` holds the text,
which is why the file is what a reader sends and the folder is not.
"""

import logging
import os
import sys
import threading
import traceback
from types import TracebackType
from typing import IO

FORMAT = "%(levelname)s %(name)s %(message)s"

# Longer than any status line the Engine writes, shorter than any paragraph
# of a Source. The shell's writer applies the same limit to what it relays;
# two gates rather than one, because the file is designed to be sent.
MAX_LINE_CHARS = 1000


def withheld(line: str) -> str:
    """`line` as the log records it: itself with the reader's home spelled
    `~`, or its length alone."""
    if len(line) <= MAX_LINE_CHARS:
        return unhomed(line)
    return (
        f"[a {len(line)}-character line was withheld: "
        "the log keeps lengths, never text]"
    )


def unhomed(line: str) -> str:
    """`line` with every spelling of the reader's home directory as `~`.
    A path the Engine names on its own — the data folder, the bundled
    runtime — starts with the home, and the home holds the reader's name."""
    home = os.path.expanduser("~")
    if len(home) <= 1:
        return line
    return line.replace(home, "~")


class WithholdingFormatter(logging.Formatter):
    """The line format above, applied to every line a record produces — a
    traceback is many — with each line past `MAX_LINE_CHARS` replaced by
    its length, and an exception recorded as its frames (by file name, not
    path) and its type, never its message: `ValueError(block.text)` is a
    short line, and the length rule alone would keep it."""

    def format(self, record: logging.LogRecord) -> str:
        # `logging` caches a formatted traceback on the record; another
        # handler's cache would carry the message this one withholds.
        record.exc_text = None
        return "\n".join(withheld(line) for line in super().format(record).splitlines())

    def formatException(
        self,
        ei: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        exc_type, _, tb = ei
        name = exc_type.__qualname__ if exc_type is not None else "Exception"
        # A frame names its source file by absolute path, which runs
        # through the bundle's location and the reader's home; the file's
        # own name is enough to find the line.
        stack = traceback.extract_tb(tb)
        for frame in stack:
            frame.filename = os.path.basename(frame.filename)
        frames = "".join(stack.format())
        return (
            "Traceback (most recent call last):\n"
            f"{frames}{name}: [the message was withheld: "
            "the log keeps types, never text]"
        )


def record_uncaught(
    exc_type: type[BaseException],
    value: BaseException,
    tb: TracebackType | None,
) -> None:
    """`sys.excepthook` once logging is configured: an exception nothing
    caught is a record like any other, so the formatter withholds its
    message and shortens its frames, where the interpreter's own hook would
    print both to stderr whole."""
    logging.getLogger("readily_engine").critical(
        "uncaught exception", exc_info=(exc_type, value, tb)
    )


def record_uncaught_in_thread(args: threading.ExceptHookArgs) -> None:
    """`threading.excepthook`, the same way: the worker's threads end in
    the log, never on a raw stderr."""
    if args.exc_type is SystemExit or args.exc_value is None:
        return
    name = args.thread.name if args.thread is not None else "?"
    logging.getLogger("readily_engine").critical(
        "uncaught exception in thread %s",
        name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def configure_logging(
    stream: IO[str] = sys.stderr, level: int = logging.INFO
) -> logging.Handler:
    """Routes every logger in the process, uvicorn's included, through one
    withholding handler on `stream`, and every uncaught exception through
    the same handler. Returns the handler so a test can remove it again."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(WithholdingFormatter(FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    sys.excepthook = record_uncaught
    threading.excepthook = record_uncaught_in_thread
    return handler
