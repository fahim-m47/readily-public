"""The Engine owns its bind (threat model B3): it binds `:0` itself and
announces the result, so the port never exists un-owned. A supervisor that
picked a port and released it would leave a window in which another local
user's process could take it — and be handed the launch token by the first
health probe."""

import pytest
from fastapi import FastAPI

from readily_engine.server.serve import (
    PORT_ANNOUNCEMENT,
    announce,
    bind,
    server_config,
)


@pytest.fixture
def bound():
    sock = bind(0)
    with sock:
        yield sock


def test_binding_port_zero_yields_a_real_loopback_port(bound):
    host, port = bound.getsockname()

    assert host == "127.0.0.1"
    assert port != 0


def test_the_announced_port_is_already_owned(bound):
    # The security property itself: by announcement time the bind is held,
    # so a rival bind — a would-be token thief — is refused by the kernel.
    #
    # This is why `bind` listens before it returns. `SO_REUSEADDR` lets a
    # second process bind a merely-bound port on Linux and refuses it only
    # once the port is in LISTEN, so drop the listen and this passes on a
    # Mac while the window reopens everywhere else. CI runs the Engine on
    # Linux precisely so that regression is loud rather than local.
    port = bound.getsockname()[1]

    with pytest.raises(OSError):
        bind(port)


def test_the_announcement_is_one_line_the_supervisor_can_parse(capsys):
    sock = bind(0)
    with sock:
        announce(sock)
        port = sock.getsockname()[1]

    assert capsys.readouterr().out == f"{PORT_ANNOUNCEMENT}{port}\n"


def test_uvicorn_logs_through_the_root_logger_and_keeps_no_access_log():
    # The root logger is the log file a reader sends; an access log would
    # fill it with health probes and name the Narration being fetched.
    config = server_config(FastAPI(), 0)
    assert config.log_config is None
    assert config.access_log is False
