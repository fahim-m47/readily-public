"""Cutting short synthesis on the generation thread without replacing it."""

import sys
import threading
from collections.abc import Callable
from types import FrameType

from readily_engine.narration.preparation import Cancelled


class _Interrupted(BaseException):
    """Raised into the generation thread. A BaseException so the `except
    Exception` a model library wraps its own steps in cannot swallow it."""


class Interruptible:
    """Lets another thread cut short the work the generation thread is doing.

    The worker runs each synthesis — model loading included — through `run`,
    and anything that makes that work stale calls `interrupt`. The thread
    itself is never stopped or replaced: MLX segfaults when a generation
    thread exits. Instead a trace hook, installed only while an interrupt is
    pending, raises into the thread at its next Python call, so a model's
    token loop stops within a step. A single native call — one ONNX run, one
    MLX kernel — cannot be entered and finishes first. Until then nothing is
    hooked, so synthesis pays nothing for being interruptible.

    The standard library is never interrupted: its locks and conditions
    assume their own calls cannot fail, and raising there could leave one
    held for good.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._region: tuple[int, Callable[[], bool]] | None = None
        self._hooked = False

    def run[T](self, work: Callable[[], T], cancelled: Callable[[], bool]) -> T:
        """Run `work` on this thread, raising `Cancelled` if it was cut short
        or `cancelled` already holds. `interrupt` acts only while `cancelled`
        holds, so a caller whose work must never be lost passes `lambda:
        False`."""
        region = (threading.get_ident(), cancelled)
        with self._lock:
            self._region = region
        try:
            # After entering: an `interrupt` that came before found nothing
            # to hook, so the staleness it acted on has to be seen here.
            if cancelled():
                raise Cancelled
            return work()
        except _Interrupted:
            raise Cancelled from None
        finally:
            with self._lock:
                self._region = None
                hooked, self._hooked = self._hooked, False
            sys.settrace(None)
            if hooked:
                threading.settrace_all_threads(None)

    def interrupt(self) -> None:
        """Cut short the work in `run`, from any thread, if it is cancelled.

        Call it after making the work stale, and never while holding a lock
        the work's `cancelled` takes."""
        with self._lock:
            region = self._region
            if region is None or self._hooked:
                return
            target, cancelled = region
            if not cancelled():
                return

            def trace(frame: FrameType, event: str, arg: object) -> None:
                if (
                    threading.get_ident() == target
                    and self._region is region
                    and _module_root(frame) not in sys.stdlib_module_names
                ):
                    # A raising trace function is unset for its thread, so
                    # this fires once.
                    raise _Interrupted
                return None

            # Under the lock, so `run` cannot leave between the check above
            # and the install and leave the hook behind.
            self._hooked = True
            threading.settrace_all_threads(trace)


def _module_root(frame: FrameType) -> str:
    return str(frame.f_globals.get("__name__", "")).partition(".")[0]
