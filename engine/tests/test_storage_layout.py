"""The Readily data tree keeps permanent History in backups, not blobs."""

# This integration test invokes only fixed-argv /usr/bin/tmutil calls; Semgrep
# still checks the invocations themselves against that allowlist.
import subprocess  # nosemgrep: engine-no-process-spawn
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from readily_engine.storage.layout import (
    LayoutError,
    exclude_reproducible_directories,
    exclude_reproducible_directories_in_background,
    initialize_layout,
)

_REPRODUCIBLE = ("models", "segments", "engine", "bytecode")


def test_layout_creates_the_whole_tree(tmp_path):
    layout = initialize_layout(tmp_path)

    assert layout.database == tmp_path / "readily.db"
    assert layout.models == tmp_path / "models"
    assert layout.staging == tmp_path / "staging"
    assert layout.segments == tmp_path / "segments"
    assert layout.engine == tmp_path / "engine"
    assert layout.bytecode == tmp_path / "bytecode"
    assert layout.logs == tmp_path / "logs"
    assert all(path.is_dir() for path in (*layout.excluded_directories, layout.logs))
    assert layout.staging.is_dir()


def test_layout_initialization_is_idempotent(tmp_path):
    first = initialize_layout(tmp_path)
    second = initialize_layout(tmp_path)

    assert second == first


def test_only_reproducible_directories_leave_the_user_backups(tmp_path):
    excluded: list[Path] = []

    layout = initialize_layout(tmp_path)
    exclude_reproducible_directories(layout, exclude_from_time_machine=excluded.append)

    assert excluded == [tmp_path / name for name in _REPRODUCIBLE]
    assert layout.database not in excluded
    assert layout.staging not in excluded


@pytest.mark.parametrize(
    "failure",
    [
        OSError("tmutil is unavailable"),
        subprocess.CalledProcessError(1, ["/usr/bin/tmutil"]),
        subprocess.TimeoutExpired(["/usr/bin/tmutil"], 10),
    ],
)
def test_a_refused_exclusion_does_not_stop_the_rest(tmp_path, failure):
    attempted: list[Path] = []

    def fail(directory: Path) -> None:
        attempted.append(directory)
        raise failure

    layout = initialize_layout(tmp_path)
    exclude_reproducible_directories(layout, exclude_from_time_machine=fail)

    assert attempted == list(layout.excluded_directories)


def test_a_slow_exclusion_never_delays_the_caller(tmp_path):
    started = threading.Event()
    released = threading.Event()
    excluded: list[Path] = []

    def wait_to_be_released(directory: Path) -> None:
        started.set()
        released.wait(timeout=5)
        excluded.append(directory)

    layout = initialize_layout(tmp_path)
    thread = exclude_reproducible_directories_in_background(
        layout, exclude_from_time_machine=wait_to_be_released
    )

    assert started.wait(timeout=5)
    assert excluded == []

    released.set()
    thread.join(timeout=5)
    assert excluded == list(layout.excluded_directories)


def test_off_macos_there_is_no_time_machine_to_ask(tmp_path, monkeypatch, caplog):
    # Linux has no tmutil. Trying it would log a warning for every directory,
    # every boot, about a backup system the reader does not have.
    def never(*args, **kwargs):
        raise AssertionError("tmutil was run off macOS")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", never)
    layout = initialize_layout(tmp_path)

    exclude_reproducible_directories(layout)

    assert caplog.records == []


def test_layout_refuses_to_start_when_the_tree_cannot_be_created(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    with pytest.raises(LayoutError, match="application-data tree"):
        initialize_layout(blocked)


@pytest.mark.skipif(sys.platform != "darwin", reason="tmutil is macOS-only")
def test_real_tmutil_excludes_reproducible_data_but_not_database():
    application_support = Path.home() / "Library" / "Application Support"
    with tempfile.TemporaryDirectory(
        prefix="Readily-tmutil-test-", dir=application_support
    ) as temporary_root:
        layout = initialize_layout(Path(temporary_root))
        layout.database.touch()
        # Twice, because every launch runs it: the second pass must find the
        # exclusions already in place and leave them that way rather than
        # re-walking the tree.
        exclude_reproducible_directories(layout)
        exclude_reproducible_directories(layout)

        for directory in layout.excluded_directories:
            result = subprocess.run(
                ["/usr/bin/tmutil", "isexcluded", str(directory)],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert "[Excluded]" in result.stdout

        database = subprocess.run(
            ["/usr/bin/tmutil", "isexcluded", str(layout.database)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "[Included]" in database.stdout
