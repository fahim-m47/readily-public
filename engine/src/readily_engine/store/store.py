"""Download-verify-promote, in one place (ADR 0003 §3, threat model B1).

The invariant this module owns: **a promoted model directory exists ⇔ its
contents are hash-verified and complete** — `install` and `promote_staged`
are the only writers of promoted paths, both through the same
verify-then-rename step, and the promotion itself is a single same-volume
`os.rename`, so a process killed at any moment leaves either a staging
directory (not promoted, retried later) or a fully verified model, never
anything between.

A promoted directory holds two kinds of file. The Manifest's pinned files
are the download, verified against their hashes and never written by
anything else. The store's own files (`LICENSE` from the bundled text,
`Notice` from the obligations table, ADR 0009 §3; a graph derived from a
pinned export, ADR 0011) are generated, not fetched, written into staging
before promotion, and — the one write into a promoted directory —
backfilled by `repair` at Engine startup beside models an earlier Engine
promoted without them, each by a same-directory rename so nothing is ever
half written where the Engine loads from.

Promotion is where the invariant is established; `repair` is where it is
re-checked. A promoted directory can stop being complete after the fact —
a user or another tool removes a file from it, or a later Manifest pins a
file under an already-promoted `name:tag` — so startup verifies every
pinned file of every promoted directory, writes the graph the Engine loads
beside it, and retires one that fails either. The entry is then not
installed, and the next download stages it again,
verifies every pinned file, and promotes: full verified replacement, the
same path a fresh install takes. A broken directory the store cannot
retire — its parent is not writable, so the rename has nowhere to go —
gets a marker file inside it instead, and `installed` reads the
marker: the answer stays on disk, where any process reading the data dir
finds the same one. Only when neither directory can be written does a
broken model still look installed, and `repair` says so in the log. The
bundled licence text is never written in place of a pinned file: it would
fail the pinned hash, and for MIT and BSD the holder's notice it may lack
*is* the licence.

The network step is an injected callable (`readily_engine.download` in
production); this module itself only touches the filesystem.
"""

import hashlib
import logging
import os
import re
import shutil
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from functools import partial
from pathlib import Path
from uuid import uuid4

from readily_engine.catalog import BROKEN_MARKER, PinnedArtifact
from readily_engine.catalog.licences import files_the_store_writes
from readily_engine.storage.layout import DataLayout
from readily_engine.store.derived import output_tail, write_derived

# A fetch fills a staging directory with the entry's files, resumably; the
# store never cares how. It may raise (network loss) — staging survives so
# the next attempt resumes instead of restarting.
Fetch = Callable[[PinnedArtifact, Path], None]

# One file the store generates: its path inside the model directory, the
# SHA-256 of what it should hold, and how to write it to a given path.
StoreFile = tuple[str, str, Callable[[Path], None]]

logger = logging.getLogger(__name__)

_HASH_CHUNK_BYTES = 1 << 20


class VerificationError(Exception):
    """A staged file is missing or does not match its manifest pin."""


class StoreUnwritable(Exception):
    """A broken promoted model sits in a directory the store cannot write,
    so a download would fail at promotion for the same reason repair could
    not retire it. `folder` is the directory; what to tell the reader is
    the caller's."""

    def __init__(self, folder: Path) -> None:
        super().__init__(f"{folder} is not writable")
        self.folder = folder


# `BROKEN_MARKER` is the file `repair` leaves inside a broken promoted
# directory it could not retire. Its presence is what `installed` reads;
# its content is why. The Manifest refuses an artifact path that starts
# with the name, so no pinned file can pass for it.


class ModelStore:
    """The Engine-owned model store under one data dir.

    Layout: `models/<name>/<tag>/` holds promoted models and nothing else;
    `staging/<name>/<tag>/` holds in-flight downloads. Both live under the
    same data dir so promotion is an atomic rename, and deleting a model is
    deleting one directory.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._layout = DataLayout.under(data_dir)

    def promoted_dir(self, entry: PinnedArtifact) -> Path:
        """The one path the Engine may load this entry from."""
        return self._layout.models / entry.name / entry.tag

    def staging_dir(self, name: str, tag: str) -> Path:
        """Where an in-flight download for one `name:tag` lands.

        Named by identity rather than by entry because `curation/` stages a
        model *before* there is an entry to ask with — hashing what it
        staged is how the entry gets its pins — and both packages
        have to mean the same directory or the download happens twice.
        """
        return self._layout.staging / name / tag

    def installed(self, entry: PinnedArtifact) -> bool:
        """Whether the Engine may load this entry: a promoted directory,
        without the marker `repair` leaves in one it found broken and
        could not retire."""
        promoted = self.promoted_dir(entry)
        return promoted.is_dir() and not (promoted / BROKEN_MARKER).exists()

    def verify_installed(self, entry: PinnedArtifact) -> None:
        """Refuse stale promoted bytes before curation labels their output.

        Unlike startup repair, this check never removes files from the store.
        """
        first = next(self._unverified(entry, self.promoted_dir(entry)), None)
        if first is not None:
            raise VerificationError(" ".join(first))

    def install(
        self,
        entry: PinnedArtifact,
        fetch: Fetch,
        on_phase: Callable[[str], None] = lambda phase: None,
    ) -> None:
        """Fetch into staging, verify every file, atomically promote.

        Raises `VerificationError` (staging discarded — a corrupted file
        cannot heal by resuming) or whatever `fetch` raises (staging kept
        so a retry resumes).

        An already promoted model is not fetched again: `repair` retired
        every promoted directory that failed verification at startup, so
        one that is still promoted is verified and complete. One that
        `repair` marked instead is retired here, now that the reader may
        have fixed the folder; when the rename still refuses, this raises
        `StoreUnwritable` before a byte is fetched, because promotion would
        rename into the same directory, and the reader's fix is to the
        folder, not to the download.
        """
        if self.installed(entry):
            return
        # Not installed but still there is exactly a marked directory.
        try:
            if self.retire(entry):
                self.purge_retired(entry)
        except OSError as error:
            raise StoreUnwritable(self.promoted_dir(entry).parent) from error
        staging = self.staging_dir(entry.name, entry.tag)
        staging.mkdir(parents=True, exist_ok=True)

        on_phase("downloading")
        fetch(entry, staging)

        on_phase("verifying")
        self._verify_and_promote(entry, staging)

    def repair(self, entries: Iterable[PinnedArtifact]) -> None:
        """Make every promoted model among `entries` what its name claims:
        retire one that no longer holds every pinned file at its pinned
        size and hash, and write the files the store generates beside one
        that lacks them or holds an earlier table's text.

        Run once at Engine startup, before anything else touches the store,
        so `installed` is truthful from the first request. Hashing every
        promoted model costs about a second per two gigabytes installed,
        inside the supervisor's start deadline for today's Catalog.
        Idempotent. A model is retired when a pinned file fails its hash or
        the graph the Engine loads cannot be written; a licence file the
        store cannot write is logged and left for the next start, because
        withholding a hash-verified model over a kilobyte of text would
        cost the Reader the whole download again.

        Retiring is the whole repair: nothing is fetched here, because the
        store never touches the network and the Reader chose when to
        download. Staging is left alone, so a download interrupted by the
        last shutdown still resumes.

        Writing the store's own files is the only path that writes into a
        promoted directory: a model promoted before the licence travelled
        with the weights is never installed again, so nothing else would
        write it.

        A broken directory whose parent refuses the rename is
        marked instead, with the error inside the marker, and retried at
        the next start or the next download: the marker is a record of
        what could not be done, not a verdict, so it goes when the
        directory verifies again or the rename succeeds. A directory that
        refuses both the rename and the marker is the invariant's one
        remaining hole, logged as such.
        """
        for entry in entries:
            promoted = self.promoted_dir(entry)
            # Every promoted directory, marked or not: a marked one is
            # what this pass exists to retry.
            if not promoted.is_dir():
                continue
            broken = self._broken(entry, promoted)
            if broken is None:
                self._unmark(entry, promoted)
                try:
                    self._write_files(entry, promoted, self._licence_files(entry))
                except OSError as error:
                    logger.warning(
                        "could not write the licence for %s:%s: %s",
                        entry.name,
                        entry.tag,
                        error,
                    )
                continue
            logger.warning(
                "%s:%s is not installed until downloaded again: %s",
                entry.name,
                entry.tag,
                broken,
            )
            try:
                self.retire(entry)
            except OSError as error:
                self._mark(entry, promoted, error)
                continue
            self.purge_retired(entry)

    def _mark(self, entry: PinnedArtifact, promoted: Path, error: OSError) -> None:
        """Record inside `promoted` that it is broken and could not be
        retired, so `installed` stops reporting it. Needs write access to
        the promoted directory, which the rename did not; when that is
        refused too, the model still looks installed, and the log says so
        loudly rather than hide it."""
        # Written in place, not through `_write_files`' `.part` rename:
        # only the marker's presence is read, so a half-written one is
        # as good as a whole one.
        try:
            (promoted / BROKEN_MARKER).write_text(
                f"could not be retired: {error}\n", encoding="utf-8"
            )
        except OSError as marker_error:
            logger.error(
                "%s:%s is broken but could not be retired, so it still "
                "looks installed: %s; and could not be marked: %s",
                entry.name,
                entry.tag,
                error,
                marker_error,
            )
            return
        logger.error(
            "%s:%s is broken but could not be retired, because %s is not "
            "writable: %s. It is not installed until that folder can be "
            "written and it is downloaded again.",
            entry.name,
            entry.tag,
            promoted.parent,
            error,
        )

    def _unmark(self, entry: PinnedArtifact, promoted: Path) -> None:
        """Drop the marker from a directory that verifies now."""
        marker = promoted / BROKEN_MARKER
        if not marker.exists():
            return
        try:
            marker.unlink()
        except OSError as error:
            logger.error(
                "%s:%s verifies but its marker could not be removed, so it "
                "still looks broken: %s",
                entry.name,
                entry.tag,
                error,
            )

    def _broken(self, entry: PinnedArtifact, promoted: Path) -> str | None:
        """Why the Engine cannot load `promoted`: a pinned file that fails
        verification, or a derived graph the store could not write beside
        it. None when the directory is what its name claims."""
        failed = list(self._unverified(entry, promoted))
        if failed:
            return "; ".join(f"{path} {why}" for path, why in failed)
        try:
            self._write_files(entry, promoted, self._derived_files(entry, promoted))
        except OSError as error:
            return f"its graph could not be written ({error})"
        return None

    def promote_staged(self, entry: PinnedArtifact) -> None:
        """Verify what a caller staged itself and make it the promoted
        model, replacing whatever was promoted under the same `name:tag`.

        The operation `curation/` runs on: pinning downloads every named
        file and hashes what arrived, so by the time promotion is asked
        for, the staged bytes are the very bytes the entry pins — and the
        promoted model must become unconditionally those bytes, never
        whatever an earlier install left promoted, or a weights bump would
        capture, qualify and clip the old ones.

        Raises `VerificationError` with staging discarded — the bytes
        stopped matching a hash taken of them a moment earlier, and
        re-downloading is the right answer. Either way, no retired tree is
        left waiting for an app-side delete to sweep a model's worth of
        disk.
        """
        self.retire(entry)
        try:
            self._verify_and_promote(entry, self.staging_dir(entry.name, entry.tag))
        finally:
            self.purge_retired(entry)

    def _verify_and_promote(self, entry: PinnedArtifact, staging: Path) -> None:
        # Strip first, verify second, and never the other way round: a file
        # reached through a symlinked parent directory would otherwise hash
        # clean and then be stripped as the link it is, promoting a
        # verified-but-incomplete directory. Stripping first turns that file
        # into a missing one, which verification refuses.
        self._keep_only_manifest_files(entry, staging)
        self._verify(entry, staging)
        # After the prune, which would strip these as extras, and outside
        # `_verify`: they come from the app's own table, not the download.
        self._write_files(entry, staging, self._licence_files(entry))
        self._write_files(entry, staging, self._derived_files(entry, staging))

        promoted = self.promoted_dir(entry)
        promoted.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, promoted)
        self._prune_empty(staging.parent)

    def _write_files(
        self, entry: PinnedArtifact, directory: Path, files: Iterable[StoreFile]
    ) -> None:
        """Write the given store-generated files beside the weights, where
        `directory` lacks them or holds other bytes. Each lands by a
        same-directory rename, so a kill mid-write leaves the file as it
        was, never half written — in a promoted directory, that is what
        the Engine may load from — and the `.part` a kill leaves is swept
        on the next write. Raises the first `OSError`; the caller decides
        what a failed file means for the model."""
        pinned = {directory / file.path for file in entry.files}
        for name, expected, write in files:
            target = directory / name
            unfinished = target.with_name(f"{target.name}.{uuid4().hex}.part")
            try:
                litter = re.compile(rf"{re.escape(target.name)}\.[0-9a-f]{{32}}\.part")
                for abandoned in list(target.parent.iterdir()):
                    if abandoned in pinned or not litter.fullmatch(abandoned.name):
                        continue
                    if abandoned.is_file():
                        abandoned.unlink(missing_ok=True)
            except OSError as error:
                logger.warning(
                    "could not sweep partial files for %s: %s", target, error
                )
            try:
                if target.is_file() and file_sha256(target) == expected:
                    continue
                write(unfinished)
                os.rename(unfinished, target)
            except OSError:
                with suppress(OSError):
                    unfinished.unlink(missing_ok=True)
                raise

    def _licence_files(self, entry: PinnedArtifact) -> Iterator[StoreFile]:
        """The licence text and notice the store writes beside the weights
        (ADR 0009 §3)."""
        for name, text in files_the_store_writes(entry).items():
            content = text.encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            yield name, digest, partial(Path.write_bytes, data=content)

    def _derived_files(
        self, entry: PinnedArtifact, directory: Path
    ) -> Iterator[StoreFile]:
        """The graphs the Engine loads, derived from a pinned export in
        `directory` (ADR 0011)."""
        for derived in entry.derived_files:
            source = directory / derived.source
            tail = output_tail(derived.outputs)
            digest = file_sha256(source, tail=tail)
            yield derived.path, digest, partial(write_derived, source, tail=tail)

    def delete(self, entry: PinnedArtifact) -> bool:
        """Remove the promoted model (and any stale staging). Returns
        whether a promoted model existed. `retire` then `purge`, for
        callers with no lock to worry about."""
        existed = self.retire(entry)
        self.purge(entry)
        return existed

    def retire(self, entry: PinnedArtifact) -> bool:
        """The fast half of a delete: one rename that moves the promoted
        model out from under its `name:tag`, so `installed` stops seeing it
        the moment the caller's lock allows. Returns whether a promoted
        model existed. A kill after this leaves only a `.deleting` tree
        that `installed` never reports and the next `purge` sweeps up.

        Renamed beside itself, never into staging: moving a directory to
        another parent rewrites its `..` entry, which needs write access
        to the directory being moved, and the directory `repair` most
        wants gone is the one it could not write into. A same-parent
        rename needs only the parent. Named uniquely per retire so the
        rmtree in `purge` — which runs with no locks held — can never race
        a new download promoting the same entry."""
        promoted = self.promoted_dir(entry)
        if not promoted.is_dir():
            return False
        os.rename(
            promoted, promoted.with_name(f"{promoted.name}-{uuid4().hex}.deleting")
        )
        return True

    def purge(self, entry: PinnedArtifact) -> None:
        """The slow half of a delete: rmtree what `retire` set aside (and
        any `.deleting` litter a kill left behind), plus stale staging.
        Call with no locks held — a multi-GB rmtree takes seconds."""
        self.purge_retired(entry)
        self._discard_staging(entry)
        self._prune_empty(self.promoted_dir(entry).parent)

    def purge_retired(self, entry: PinnedArtifact) -> None:
        """`purge` without the staging half: sweep a retired tree while a
        download is still worth keeping.

        `promote_staged` retires the promoted model before verifying the
        staged bytes, so a failure between the two leaves a retired tree
        with nobody to sweep it — but the staging directory beside it may
        hold a complete download that a retry would otherwise repeat.
        """
        promoted = self.promoted_dir(entry)
        for retired in promoted.parent.glob(f"{promoted.name}-*.deleting"):
            shutil.rmtree(retired, ignore_errors=True)
        self._prune_empty(promoted.parent)
        # Builds before the same-parent rename retired into staging; a
        # kill between their retire and purge left a tree only this sweep
        # can still find.
        staging = self.staging_dir(entry.name, entry.tag)
        for retired in staging.parent.glob(f"{staging.name}-*.deleting"):
            shutil.rmtree(retired, ignore_errors=True)
        self._prune_empty(staging.parent)

    def disk_usage(self, entry: PinnedArtifact) -> int:
        """Bytes the promoted model occupies; 0 when not installed, a
        marked directory included: its bytes are not the reader's to free
        until the folder can be written, so the Storage screen does not
        offer them."""
        if not self.installed(entry):
            return 0
        return _tree_bytes(self.promoted_dir(entry))

    def staged_bytes(self, entry: PinnedArtifact) -> int:
        """Bytes so far in staging — download progress, including the
        downloader's partial files."""
        return _tree_bytes(self.staging_dir(entry.name, entry.tag))

    def _verify(self, entry: PinnedArtifact, staging: Path) -> None:
        first = next(self._unverified(entry, staging), None)
        if first is not None:
            self._discard_staging(entry)
            raise VerificationError(" ".join(first))

    def _unverified(
        self, entry: PinnedArtifact, directory: Path
    ) -> Iterator[tuple[str, str]]:
        """Yield each pinned file `directory` lacks or holds wrong, with why
        — the one check behind promotion and startup repair, so both mean
        the same thing by "verified"."""
        for file in entry.files:
            path = directory / file.path
            # lstat before reading: a symlink's target could hash clean
            # while the link points wherever it likes.
            if path.is_symlink() or not path.is_file():
                yield file.path, "is missing"
                continue
            # A file that cannot be read cannot be shown to match its pin,
            # so it fails like one that does not.
            try:
                if path.stat().st_size != file.size_bytes:
                    yield file.path, "has the wrong size"
                elif file_sha256(path) != file.sha256:
                    yield file.path, "does not match its pin"
            except OSError as error:
                yield file.path, f"could not be read ({error.strerror})"

    def _keep_only_manifest_files(self, entry: PinnedArtifact, staging: Path) -> None:
        """Strip everything the manifest does not name — the downloader's
        resume metadata, or an upstream extra — so promotion moves exactly
        the verified files (threat model B1)."""
        keep = {staging / file.path for file in entry.files}
        deepest_first = sorted(
            staging.rglob("*"), key=lambda p: len(p.parts), reverse=True
        )
        for path in deepest_first:
            if path.is_symlink() or path.is_file():
                if path not in keep:
                    path.unlink()
            elif path.is_dir():
                with suppress(OSError):
                    path.rmdir()

    def _discard_staging(self, entry: PinnedArtifact) -> None:
        staging = self.staging_dir(entry.name, entry.tag)
        shutil.rmtree(staging, ignore_errors=True)
        self._prune_empty(staging.parent)

    def _prune_empty(self, directory: Path) -> None:
        if directory != self.data_dir:
            with suppress(OSError):
                directory.rmdir()


def file_sha256(path: Path, *, tail: bytes = b"") -> str:
    """The SHA-256 of a file, chunked so a multi-GB model does not have to
    fit in memory, with `tail` counted as if appended — what a derived
    file hashes to, read from its source alone."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    digest.update(tail)
    return digest.hexdigest()


def _tree_bytes(root: Path) -> int:
    """Bytes below `root`, measured while it may be moving under us.

    Staging is walked for progress while the downloader renames partials
    into place, so a path listed a moment ago may already be gone. That is
    an observation, not a failure: skip it and keep counting, rather than
    letting a progress read raise into an SSE stream or a `GET /v1/models`.
    """
    total = 0
    # os.walk rather than rglob: it drops a directory that vanishes
    # mid-walk instead of raising out of the iterator, and a missing
    # directory is simply an empty walk.
    for directory, _subdirectories, files in os.walk(root):
        for name in files:
            with suppress(OSError):
                total += (Path(directory) / name).stat().st_size
    return total
