"""Engine entrypoint: wiring only.

Every decision with a security face lives in a package the threat model's
review triggers watch — HF environment policy in `download/`, token
sourcing, auth, and the bind in `server/`. Keep it that way.
"""

import os
import sys

from readily_engine.download.environment import (
    configure_hf_environment,
    default_data_dir,
)
from readily_engine.logs import configure_logging
from readily_engine.server.auth import launch_token
from readily_engine.server.lifetime import exit_when_supervisor_exits, supervised
from readily_engine.server.serve import serve


def main() -> None:
    configure_logging()
    configure_hf_environment(default_data_dir())

    if supervised():
        exit_when_supervisor_exits()

    raw_port = os.environ.get("READILY_ENGINE_PORT", "0")
    try:
        port = int(raw_port)
    except ValueError:
        sys.exit(f"READILY_ENGINE_PORT must be an integer, got {raw_port!r}")

    serve(launch_token(), port)
