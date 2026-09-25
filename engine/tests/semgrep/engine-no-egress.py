"""Regression fixtures for the Engine's no-egress Semgrep rules."""

import os
import socket
import sqlite3

# ruleid: engine-no-process-spawn
import subprocess

# ruleid: engine-no-dynamic-import
from importlib import import_module

# ruleid: engine-no-dynamic-import
from importlib.metadata import version

# ruleid: engine-no-dynamic-import
from importlib.util import module_from_spec

# The name the sanctioned `tmutil addexclusion` call pins its timeout to, as
# `storage.layout` spells it.
# ok: engine-bounded-exclusion-timeout
EXCLUSION_TIMEOUT_SECONDS = 120


def local_database() -> None:
    # ok: engine-no-egress-outside-download
    sqlite3.connect(":memory:")


def network_connection() -> None:
    connection = socket.socket()
    # ruleid: engine-no-egress-outside-download
    connection.connect(("example.com", 443))


def arbitrary_process() -> None:
    # ruleid: engine-no-process-spawn
    subprocess.run(["/usr/bin/curl", "https://example.com"], check=True)


def unapproved_system_tool_operation() -> None:
    # ruleid: engine-no-process-spawn
    subprocess.run(["/usr/bin/afconvert", "--help"], check=True)


def unapproved_export_codec() -> None:
    # No MP3 (ADR 0004 §4): the allowlist is per-invocation, so a second
    # codec is a rule amendment, not a code change.
    # ruleid: engine-no-process-spawn
    subprocess.run(
        [
            "/usr/bin/afconvert",
            "input.wav",
            "-f",
            "mp3f",
            "-d",
            ".mp3",
            "-b",
            "64000",
            "output.mp3",
        ],
        check=True,
        capture_output=True,
    )


def executable_override() -> None:
    # ruleid: engine-no-process-spawn
    subprocess.run(
        ["/usr/bin/tmutil", "addexclusion", "/tmp/cache"],
        check=True,
        capture_output=True,
        text=True,
        timeout=EXCLUSION_TIMEOUT_SECONDS,
        executable="/usr/bin/curl",
    )


def unpinned_exclusion_timeout() -> None:
    """The sanctioned call is the whole call: an argv that matches but a
    timeout spelled some other way is a different call, and reviewed as one."""
    # ruleid: engine-no-process-spawn
    subprocess.run(
        ["/usr/bin/tmutil", "addexclusion", "/tmp/cache"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def subprocess_alias() -> None:
    # ruleid: engine-no-process-spawn
    runner = subprocess.run
    runner(["/usr/bin/curl", "https://example.com"], check=True)


def direct_os_process_apis() -> None:
    # ruleid: engine-no-process-spawn
    os.spawnv(os.P_WAIT, "/usr/bin/curl", ["curl", "https://example.com"])
    # ruleid: engine-no-process-spawn
    os.execv("/usr/bin/curl", ["curl", "https://example.com"])


def sanctioned_system_tools() -> None:
    # ok: engine-no-process-spawn
    subprocess.run(
        ["/usr/bin/tmutil", "addexclusion", "/tmp/cache"],
        check=True,
        capture_output=True,
        text=True,
        timeout=EXCLUSION_TIMEOUT_SECONDS,
    )
    # ok: engine-no-process-spawn
    subprocess.run(
        ["/usr/bin/tmutil", "isexcluded", "/tmp/cache"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    # ok: engine-no-process-spawn
    subprocess.run(
        [
            "/usr/bin/afconvert",
            "input.wav",
            "-f",
            "flac",
            "-d",
            "flac",
            "output.flac",
        ],
        check=True,
        capture_output=True,
    )
    # ok: engine-no-process-spawn
    subprocess.run(
        [
            "/usr/bin/afconvert",
            "input.wav",
            "-f",
            "m4af",
            "-d",
            "aac",
            "-b",
            "64000",
            "output.m4a",
        ],
        check=True,
        capture_output=True,
    )


def unbounded_exclusion_timeout() -> None:
    """The exclusion runs on a daemon thread, so its timeout is the only thing
    that ever ends a wedged `tmutil`. An hour is not a timeout."""
    # ruleid: engine-bounded-exclusion-timeout
    EXCLUSION_TIMEOUT_SECONDS = 3600  # noqa: F841


def block_by_string(architecture: str) -> object:
    """A registry populated by import machinery (ADR 0014) is a model that
    ships without a static import for the licence gate or reviewers to see."""
    # ruleid: engine-no-dynamic-import
    return import_module(f"readily_engine.loading.{architecture}")


def block_by_path(path):
    """The same escape through a file path instead of a module name."""
    # ruleid: engine-no-dynamic-import
    import importlib.util

    # ruleid: engine-no-dynamic-import
    spec = importlib.util.spec_from_file_location("block", path)
    # ruleid: engine-no-dynamic-import
    return module_from_spec(spec)


def installed_version(name):
    """Reading a package's metadata loads nothing; the tools report it."""
    # ok: engine-no-dynamic-import
    return version(name)
