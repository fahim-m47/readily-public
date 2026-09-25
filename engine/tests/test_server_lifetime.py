"""A supervised Engine must not outlive its supervisor: an orphaned Engine
is a token-guarded loopback listener with nobody left to own it (threat
model B3). The supervisor holds one end of a pipe; its closing is the
signal, and it closes however the app dies — quit, crash, or force-quit."""

import os
import threading

import pytest

from readily_engine.server.lifetime import (
    exit_when_supervisor_exits,
    supervised,
    wait_for_supervisor_exit,
)


@pytest.fixture
def pipe():
    """A supervisor's pipe: the Engine's read end, and a handle that closes
    the supervisor's write end on demand."""
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    with reader:
        yield reader, lambda: os.close(write_fd)


def test_waiting_ends_when_the_supervisor_lets_go(pipe):
    reader, close_supervisor_end = pipe
    close_supervisor_end()

    wait_for_supervisor_exit(reader)


def test_waiting_continues_while_the_supervisor_lives(pipe):
    reader, close_supervisor_end = pipe
    waiting = threading.Thread(
        target=wait_for_supervisor_exit, args=(reader,), daemon=True
    )
    waiting.start()

    waiting.join(timeout=0.2)
    assert waiting.is_alive(), "the Engine must keep running while the app does"

    close_supervisor_end()
    waiting.join(timeout=2)
    assert not waiting.is_alive()


def test_the_watchdog_ends_the_process_when_the_supervisor_goes(pipe):
    reader, close_supervisor_end = pipe
    ended = threading.Event()

    watchdog = exit_when_supervisor_exits(reader, on_exit=ended.set)
    close_supervisor_end()

    assert ended.wait(timeout=2), "EOF on the supervisor's pipe must end the Engine"
    watchdog.join(timeout=2)


def test_the_watchdog_does_not_block_interpreter_shutdown(pipe):
    reader, _ = pipe

    watchdog = exit_when_supervisor_exits(reader, on_exit=lambda: None)

    assert watchdog.daemon


def test_supervision_is_declared_by_the_supervisor_alone(monkeypatch):
    # Standalone `uv run readily-engine` has a terminal on stdin, and
    # treating a terminal's EOF as "the app quit" would be wrong.
    monkeypatch.delenv("READILY_ENGINE_SUPERVISED", raising=False)
    assert supervised() is False

    monkeypatch.setenv("READILY_ENGINE_SUPERVISED", "1")
    assert supervised() is True
