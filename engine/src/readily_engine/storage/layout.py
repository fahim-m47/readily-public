"""The Engine's application-data layout and Time Machine policy."""

import logging
import subprocess  # nosemgrep: engine-no-process-spawn
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# `tmutil addexclusion` walks what it is given, and one of the directories it
# is given is the provisioned Python environment: ~15,000 files, ~11s on a
# warm SSD. The old 10s budget killed that call every time, so the exclusion
# it was meant to apply never landed. Generous because nothing waits on it —
# `exclude_reproducible_directories` runs after the Engine is already
# serving — while still bounded, so a wedged `tmutil` is a stuck thread and
# not a stuck process.
EXCLUSION_TIMEOUT_SECONDS = 120


class LayoutError(RuntimeError):
    """The required local-data policy could not be established."""


ExclusionRunner = Callable[[Path], object]


@dataclass(frozen=True)
class DataLayout:
    """Paths the Engine owns below Readily's application-data directory.

    The one place the tree is named. `ModelStore` reads `models` and
    `staging` from here rather than rebuilding them from its data dir, so
    the directories the store writes and the directories Time Machine is
    told to skip cannot drift apart. Staging is named here too, but unlike
    completed model data it stays in the user's backups.
    """

    root: Path
    database: Path
    models: Path
    staging: Path
    segments: Path
    engine: Path
    bytecode: Path
    logs: Path

    @classmethod
    def under(cls, root: Path) -> "DataLayout":
        """Name the tree below `root` without touching the filesystem."""
        return cls(
            root=root,
            database=root / "readily.db",
            models=root / "models",
            staging=root / "staging",
            segments=root / "segments",
            engine=root / "engine",
            bytecode=root / "bytecode",
            logs=root / "logs",
        )

    @property
    def excluded_directories(self) -> tuple[Path, ...]:
        """Reproducible directories that must stay out of Time Machine."""
        return (self.models, self.segments, self.engine, self.bytecode)


def _exclude_from_time_machine(directory: Path) -> None:
    """Exclude `directory`, unless it already is. Off macOS there is no Time
    Machine, so there is nothing to ask.

    `addexclusion` re-walks the whole directory every time it is called, at
    the cost `EXCLUSION_TIMEOUT_SECONDS` describes, for a result that has
    not changed since the first launch. `isexcluded` answers from the
    directory's own metadata and returns at once, which is why it is safe
    to ask first and why the short timeout below is the right one for it.
    """
    if sys.platform != "darwin":
        return
    already = subprocess.run(
        ["/usr/bin/tmutil", "isexcluded", str(directory)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if "[Excluded]" in already.stdout:
        return
    subprocess.run(
        ["/usr/bin/tmutil", "addexclusion", str(directory)],
        check=True,
        capture_output=True,
        text=True,
        timeout=EXCLUSION_TIMEOUT_SECONDS,
    )


def initialize_layout(root: Path) -> DataLayout:
    """Create Readily's durable tree below `root`.

    Required, and required *first*: every durable thing below this needs
    the directories to exist. Deliberately does nothing else — the Time
    Machine policy is `exclude_reproducible_directories`, kept separate
    because it is slow and optional and this is not.
    """
    layout = DataLayout.under(root)
    try:
        layout.root.mkdir(parents=True, exist_ok=True)
        layout.logs.mkdir(exist_ok=True)
        layout.staging.mkdir(exist_ok=True)
        for directory in layout.excluded_directories:
            directory.mkdir(exist_ok=True)
    except OSError as error:
        raise LayoutError(
            "Readily's application-data tree could not be created"
        ) from error
    return layout


def exclude_reproducible_directories(
    layout: DataLayout,
    exclude_from_time_machine: ExclusionRunner = _exclude_from_time_machine,
) -> None:
    """Ask Time Machine to skip the directories Readily can rebuild.

    A courtesy to the user's backup disk, and nothing depends on it, so
    every failure is logged and swallowed. It is also slow, which is why
    callers run it off the boot path, after the Engine is already
    serving. A `tmutil` that is missing, sandboxed,
    or slow must not be the reason the Engine never reaches its bind and
    the supervisor never learns a port.
    """
    for directory in layout.excluded_directories:
        try:
            exclude_from_time_machine(directory)
        except (OSError, subprocess.SubprocessError):
            # The folder's name, never its path: the path runs through the
            # reader's home, and this line reaches the file a reader sends.
            logger.warning(
                "Time Machine exclusion could not be applied to %s", directory.name
            )


def exclude_reproducible_directories_in_background(
    layout: DataLayout,
    exclude_from_time_machine: ExclusionRunner = _exclude_from_time_machine,
) -> threading.Thread:
    """Run `exclude_reproducible_directories` without making anyone wait.

    A daemon thread because the exclusions are worth attempting on every
    boot but never worth delaying a shutdown for. The thread is returned
    for tests to join; callers on the boot path drop it on purpose.
    """
    thread = threading.Thread(
        target=exclude_reproducible_directories,
        args=(layout, exclude_from_time_machine),
        daemon=True,
        name="readily-exclusions",
    )
    thread.start()
    return thread
