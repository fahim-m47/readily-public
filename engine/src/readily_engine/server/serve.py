"""The bind lives here: `server/` owns where the Engine listens (B3)."""

import socket

import uvicorn
from fastapi import FastAPI

from readily_engine.catalog import load_manifest
from readily_engine.download.environment import default_data_dir
from readily_engine.loading.registry import available_backends
from readily_engine.narration.export import default_audio_folder
from readily_engine.narration.narrator import EngineNarrator
from readily_engine.server.app import create_app
from readily_engine.server.entries import default_here
from readily_engine.storage.janitor import RetentionJanitor
from readily_engine.storage.layout import (
    exclude_reproducible_directories_in_background,
    initialize_layout,
)
from readily_engine.storage.storage import NarrationStorage
from readily_engine.store import ModelStore

PORT_ANNOUNCEMENT = "READILY_ENGINE_PORT="


def bind(port: int) -> socket.socket:
    """Bind *and listen on* the Engine's listening socket. The host is not a
    parameter — anything but 127.0.0.1 is a threat-model amendment (B3).
    Port 0, the supervised default, asks the OS for a free port.

    Listening here, rather than leaving it to uvicorn, is what makes the
    port exclusively ours before `announce` puts it on stdout: `SO_REUSEADDR`
    lets a second process bind a merely-bound port on Linux, but never one
    already in LISTEN. Returning a listening socket closes that window on
    every platform instead of relying on macOS being the stricter one."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # A restarted Engine must not fail to bind because the previous launch's
    # connections are still in TIME_WAIT: a standalone Engine can be pinned
    # to a fixed port with READILY_ENGINE_PORT, and a bind that loses to a
    # lingering socket reads to the user as an Engine that will not start.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen()
    return sock


def announce(sock: socket.socket) -> None:
    """Tell the supervisor (or whoever ran the Engine by hand) the bound
    port, on stdout. Announced only after the bind, so the port on the wire
    is one this process already owns — a port promised before it is bound
    is a window for another local process to take it and be handed the
    launch token by the first health probe (threat model B3)."""
    port = sock.getsockname()[1]
    print(f"{PORT_ANNOUNCEMENT}{port}", flush=True)


def server_config(app: FastAPI, port: int) -> uvicorn.Config:
    """uvicorn on the loopback port, logging through the root logger.

    uvicorn's own log config would put a second handler on stderr in a
    second format; `None` leaves its loggers propagating to the root logger
    `readily_engine.logs` configures. No access log: the shell's health
    probe would fill the file with one line a second, and a request line
    names the Narration a reader is hearing.
    """
    return uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, access_log=False
    )


def serve(token: str, port: int) -> None:
    """Serve the Engine on the loopback socket `bind` owns. `import
    uvicorn` outside this package fails the bind-only-in-server Semgrep
    rule."""
    layout = initialize_layout(default_data_dir())
    storage = NarrationStorage.open(layout.database, layout.segments)
    janitor = RetentionJanitor(storage.run_retention)
    narrator = None
    try:
        janitor.start()
        catalog = load_manifest()
        store = ModelStore(layout.root)
        store.repair(catalog.artifacts)
        backends = available_backends()
        narrator = EngineNarrator(
            catalog,
            store,
            storage,
            default_audio_folder(),
            default_here(catalog, backends),
        )
        sock = bind(port)
        announce(sock)
        # After the announcement, never before: this walks the provisioned
        # Python environment, and costs more than the supervisor's start
        # deadline has. See `EXCLUSION_TIMEOUT_SECONDS`.
        exclude_reproducible_directories_in_background(layout)
        narrator.prewarm()
        config = server_config(
            create_app(
                token,
                narrator=narrator,
                history=narrator,
                store=store,
                catalog=catalog,
                backends=backends,
            ),
            port,
        )
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        if narrator is not None:
            narrator.close()
        janitor.close()
        storage.close()
