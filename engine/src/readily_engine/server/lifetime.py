"""How long the listener lives (threat model B3).

The Engine binds a loopback port and guards it with the launch token. If it
outlives the app that started it, that listener is an orphan nobody owns —
and the tidy shutdown path cannot cover a force-quit or a crashed
supervisor, which deliver no signal at all.

So the supervisor hands the Engine one end of a pipe and holds the other
for as long as it lives. The kernel closes the supervisor's end however the
app dies; the Engine sees EOF and exits.
"""

import os
import sys
import threading
from collections.abc import Callable
from typing import BinaryIO

SUPERVISED_VAR = "READILY_ENGINE_SUPERVISED"


def supervised() -> bool:
    """Whether the Rust supervisor started this Engine and is holding the
    pipe. A standalone `uv run readily-engine` has a terminal on stdin, and
    treating a terminal's EOF as "the app quit" would be wrong."""
    return os.environ.get(SUPERVISED_VAR) == "1"


def wait_for_supervisor_exit(pipe: BinaryIO) -> None:
    """Blocks until the supervisor's end of `pipe` closes. A supervisor that
    writes bytes is not saying anything — only EOF is the signal."""
    try:
        while pipe.read(1):
            pass
    except (OSError, ValueError):
        pass


def _terminate() -> None:
    # os._exit, not sys.exit: the watchdog fires from a non-main thread,
    # where a raised SystemExit would be swallowed, and there is nothing
    # left worth unwinding for.
    os._exit(0)


def exit_when_supervisor_exits(
    pipe: BinaryIO | None = None,
    on_exit: Callable[[], None] = _terminate,
) -> threading.Thread:
    """Starts the daemon thread that ends this process once the supervisor
    lets go of the pipe. Defaults to stdin, which is where the supervisor
    puts it."""
    watched = pipe if pipe is not None else sys.stdin.buffer

    def watch() -> None:
        wait_for_supervisor_exit(watched)
        on_exit()

    watchdog = threading.Thread(target=watch, name="supervisor-watchdog", daemon=True)
    watchdog.start()
    return watchdog
