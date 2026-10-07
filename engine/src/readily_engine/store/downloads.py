"""One download at a time, the rest waiting their turn, observable as SSE
snapshots.

Publishes through `readily_engine.wire.Observable`, the same helper the
generation worker publishes through. The store owns the verify-promote
invariant; this class only sequences it on a background thread and
translates its outcomes into wire phases.
"""

import logging
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, TypedDict

from readily_engine.catalog import CatalogEntry, PinnedArtifact, SupportModel
from readily_engine.store.store import (
    Fetch,
    ModelStore,
    StoreUnwritable,
    VerificationError,
)
from readily_engine.wire import WIRE_VERSION, Observable, WireError, wire_error

logger = logging.getLogger(__name__)

_POLL_SECONDS = 0.25

DownloadPhase = Literal["idle", "downloading", "verifying", "installed", "failed"]

QueuedAction = Literal["download", "delete"]

# What asking to delete did: deleted now, found nothing to delete, or
# queued behind the job running.
DeleteOutcome = Literal["deleted", "absent", "queued"]


class QueuedJob(TypedDict):
    """One request waiting behind the running one, as sent."""

    modelId: str
    action: QueuedAction


class FailedJob(TypedDict):
    """The latest failure of a model's download or delete, as sent."""

    modelId: str
    error: WireError


class DownloadSnapshot(TypedDict):
    """The frozen v1 download snapshot (`docs/wire.md`), as sent."""

    version: int
    phase: DownloadPhase
    modelId: str | None
    bytesTotal: int
    bytesDownloaded: int
    error: WireError | None
    queue: list[QueuedJob]
    failures: list[FailedJob]


@dataclass(frozen=True)
class _Job:
    action: QueuedAction
    entry: CatalogEntry
    artifacts: tuple[PinnedArtifact, ...] = ()


class DownloadManager:
    """Run `store.install` for one entry at a time on a background thread,
    queueing the rest in the order they were asked for.

    Also the serialization point for *deleting* a model: downloading and
    deleting are the two writers of a model's directories, so a delete
    waits in the same queue behind any download running or waiting. Run
    beside a download, a delete can rmtree its live staging, or a
    still-running download can promote a model the user just deleted. It
    waits for nothing else: beside an earlier delete's rmtree there is no
    order to keep, and a delete queued there would leave the queue the
    moment that rmtree ended, too fast for a reader polling the snapshot
    to ever see it waiting.
    """

    def __init__(self, store: ModelStore, fetch: Fetch) -> None:
        self._store = store
        self._fetch = fetch
        self._state: Observable[DownloadSnapshot] = Observable(
            DownloadSnapshot(
                version=WIRE_VERSION,
                phase="idle",
                modelId=None,
                bytesTotal=0,
                bytesDownloaded=0,
                error=None,
                queue=[],
                failures=[],
            ),
            poll_seconds=_POLL_SECONDS,
        )
        self._lock = self._state.lock
        # The job the worker is running, and the jobs waiting behind it.
        self._active: _Job | None = None
        self._queue: list[_Job] = []
        # Whether a worker owns the queue. Cleared by the worker itself,
        # under the lock, in the same step that finds the queue empty — so a
        # job queued a moment later always gets a worker of its own.
        self._draining = False
        # Each model's latest failure, kept until it is asked for again or
        # deleted, so a failure is not lost when the next job starts.
        self._failures: dict[str, WireError] = {}
        self.thread: threading.Thread | None = None

    def start(
        self, entry: CatalogEntry, *, support: tuple[SupportModel, ...] = ()
    ) -> None:
        """Download (or resume) `entry` and the `support` it needs, support
        first — now, or after everything already asked for. Asking again
        for a download already running or waiting is the same request."""
        artifacts: tuple[PinnedArtifact, ...] = (*support, entry)
        with self._lock:
            # A download that has just failed is still the active job until
            # the worker moves on; asking again then is a retry, not a repeat.
            if (
                self._last_asked(entry, self._jobs()) == "download"
                and entry.id not in self._failures
            ):
                return
            self._forget_failure(entry)
            if not self._draining and all(
                self._store.installed(artifact) for artifact in artifacts
            ):
                self._state.update(
                    phase="installed",
                    modelId=entry.id,
                    bytesTotal=_download_bytes(artifacts),
                    bytesDownloaded=_download_bytes(artifacts),
                    error=None,
                )
                return
            self._enqueue(_Job("download", entry, artifacts))

    def _jobs(self) -> list[_Job]:
        """The running job, then the waiting ones. Call under `_lock`."""
        return [*([self._active] if self._active is not None else []), *self._queue]

    def _last_asked(self, entry: CatalogEntry, jobs: list[_Job]) -> QueuedAction | None:
        """What the newest of `jobs` for `entry` does, so a repeated click
        is one request but a download asked for after a queued delete
        still runs. Call under `_lock`."""
        mine = [job for job in jobs if job.entry.id == entry.id]
        return mine[-1].action if mine else None

    def _download_pending(self) -> bool:
        """Whether a download is running or waiting — the only jobs a
        delete has to wait behind. Call under `_lock`."""
        return any(job.action == "download" for job in self._jobs())

    def _enqueue(self, job: _Job) -> None:
        """Queue `job`, starting a worker when none is draining. The first
        job is taken in this same step, so the snapshot says it is running
        by the time the request that asked for it returns. Call under
        `_lock`."""
        self._queue.append(job)
        if self._draining:
            self._state.update(queue=self._queue_wire())
            return
        self._draining = True
        self.thread = threading.Thread(
            target=self._drain,
            args=(self._next(),),
            daemon=True,
            name="readily-download",
        )
        self.thread.start()

    def _forget_failure(self, entry: CatalogEntry) -> None:
        """Call under `_lock`."""
        if self._failures.pop(entry.id, None) is not None:
            self._state.update(failures=self._failures_wire())

    def _failures_wire(self) -> list[FailedJob]:
        return [
            FailedJob(modelId=model_id, error=error)
            for model_id, error in self._failures.items()
        ]

    def _queue_wire(self) -> list[QueuedJob]:
        return [
            QueuedJob(modelId=job.entry.id, action=job.action) for job in self._queue
        ]

    def _drain(self, job: _Job | None) -> None:
        while job is not None:
            if job.action == "delete":
                self._store.purge(job.entry)
            else:
                self._download(job.entry, job.artifacts)
            job = self._next()

    def _next(self) -> _Job | None:
        """Take the oldest waiting job, or stand the worker down."""
        with self._lock:
            if not self._queue:
                self._active = None
                self._draining = False
                return None
            job = self._queue.pop(0)
            self._active = job
            if job.action == "delete":
                # Retired in the same step that takes it off the queue, so a
                # reader who sees it leave can trust the model is gone, or
                # find out why it is not; only the slow purge is left for the
                # worker. A folder that cannot be moved aside keeps its
                # model, but never the worker.
                try:
                    self._retire(job.entry)
                except OSError:
                    logger.exception("Model %s could not be deleted", job.entry.id)
                    folder = self._store.promoted_dir(job.entry).parent
                    self._failures[job.entry.id] = wire_error(
                        "store_unwritable",
                        f"Readily cannot delete this Voice Model because the "
                        f"folder {folder} is not writable. Give Readily write "
                        f"access to it.",
                    )
                self._state.update(
                    queue=self._queue_wire(), failures=self._failures_wire()
                )
                return job
            self._state.update(
                phase="downloading",
                modelId=job.entry.id,
                bytesTotal=_download_bytes(job.artifacts),
                bytesDownloaded=self._progress_bytes(job.artifacts),
                error=None,
                queue=self._queue_wire(),
            )
            return job

    def delete(self, entry: CatalogEntry) -> DeleteOutcome:
        """Delete `entry`'s installed files now, or — while a download runs
        or waits — queue the delete behind everything already asked for. A
        delete already waiting for `entry` is this request. One the worker
        is cleaning up is not: it was retired, or refused, when it left the
        queue, so this is the model already gone or the reader's retry once
        the folder is fixed, and `queued` would promise what nothing
        waiting could keep. Deciding and retiring happen under the lock
        that starts downloads, so no download can begin between them; the
        slow rmtree runs after it is released, beside whatever rmtree the
        worker is on."""
        with self._lock:
            if self._last_asked(entry, self._queue) == "delete":
                return "queued"
            if self._download_pending():
                self._enqueue(_Job("delete", entry))
                return "queued"
            deleted = self._retire(entry)
        self._store.purge(entry)
        return "deleted" if deleted else "absent"

    def withdraw(self, entry: CatalogEntry) -> bool:
        """Take `entry`'s waiting jobs out of the queue. A job already
        running carries on. Returns whether anything was waiting."""
        with self._lock:
            waiting = [job for job in self._queue if job.entry.id != entry.id]
            if len(waiting) == len(self._queue):
                return False
            self._queue = waiting
            self._state.update(queue=self._queue_wire())
            return True

    def _retire(self, entry: CatalogEntry) -> bool:
        """Retire `entry` and stop the snapshot describing it. Returns
        whether a promoted model existed. Call under `_lock`."""
        # Only the rename happens under the lock. `Observable.events` polls
        # this same lock from the asyncio loop, so an rmtree of a multi-GB
        # model here would freeze every route — /health included — long
        # enough for the supervisor to restart a healthy Engine mid-delete.
        deleted = self._store.retire(entry)
        self._forget_failure(entry)
        if deleted and self._state.snapshot()["modelId"] == entry.id:
            self._state.update(
                phase="idle",
                modelId=None,
                bytesTotal=0,
                bytesDownloaded=0,
                error=None,
            )
        return deleted

    def snapshot(self) -> DownloadSnapshot:
        return self._state.snapshot()

    def events(self) -> AsyncIterator[DownloadSnapshot]:
        """Yield changed snapshots, with live byte progress while
        downloading, plus a periodic snapshot as an SSE heartbeat."""
        return self._state.events(self._with_live_bytes)

    def _with_live_bytes(self, snapshot: DownloadSnapshot) -> DownloadSnapshot:
        """Patch staged bytes onto an in-flight download's snapshot. Byte
        counts move far faster than phases, so they are read per poll
        instead of costing a revision each."""
        with self._lock:
            artifacts = self._active.artifacts if self._active is not None else ()
        if snapshot["phase"] != "downloading" or not artifacts:
            return snapshot
        return {**snapshot, "bytesDownloaded": self._progress_bytes(artifacts)}

    def _download(
        self, entry: CatalogEntry, artifacts: tuple[PinnedArtifact, ...]
    ) -> None:
        try:
            # A broken copy the store would replace is reclaimed before the
            # space is measured, so its bytes are not held against the
            # download that frees them.
            for artifact in artifacts:
                self._store.reclaim(artifact)
            # Checked as each download starts, not when it was asked for: the
            # downloads ahead of it in the queue have used space since.
            if self._bytes_to_write(artifacts) > self._store.free_bytes():
                logger.warning("Model %s skipped: not enough free disk space", entry.id)
                self._fail(
                    entry,
                    "insufficient_disk_space",
                    "There is not enough free disk space to download this Voice Model.",
                )
                return
            for artifact in artifacts:
                self._store.install(
                    artifact,
                    self._fetch,
                    on_phase=lambda phase: self._set_phase(artifacts, phase),
                )
        except VerificationError:
            logger.exception("Model %s failed verification", entry.id)
            self._fail(
                entry,
                "verification_failed",
                "The downloaded files failed verification.",
            )
            return
        except StoreUnwritable as error:
            # Nothing was fetched: the store refused up front, and the
            # message names the folder the reader has to fix.
            logger.error("Model %s cannot be replaced: %s", entry.id, error)
            self._fail(
                entry,
                "store_unwritable",
                f"Readily cannot replace a broken copy of this model because "
                f"the folder {error.folder} is not writable. Give Readily write "
                f"access to it.",
            )
            return
        except Exception:
            logger.exception("Model %s download failed", entry.id)
            self._fail(entry, "download_failed", "The download could not be completed.")
            return
        self._state.update(
            phase="installed",
            bytesDownloaded=_download_bytes(artifacts),
            error=None,
        )

    def _set_phase(self, artifacts: tuple[PinnedArtifact, ...], phase: str) -> None:
        self._state.update(phase=phase, bytesDownloaded=self._progress_bytes(artifacts))

    def _fail(self, entry: CatalogEntry, code: str, message: str) -> None:
        error = wire_error(code, message)
        with self._lock:
            self._failures[entry.id] = error
            self._state.update(
                phase="failed",
                bytesDownloaded=self._progress_bytes(
                    self._active.artifacts if self._active is not None else ()
                ),
                error=error,
                failures=self._failures_wire(),
            )

    def _bytes_to_write(self, artifacts: tuple[PinnedArtifact, ...]) -> int:
        """What installing still puts on disk: what is left to download
        (none of an installed artifact, the unstaged rest of one that was
        resumed) plus the derived files the store writes at promotion."""
        return (
            _download_bytes(artifacts)
            - self._progress_bytes(artifacts)
            + sum(
                artifact.derived_bytes
                for artifact in artifacts
                if not self._store.installed(artifact)
            )
        )

    def _progress_bytes(self, artifacts: tuple[PinnedArtifact, ...]) -> int:
        """Staged bytes clamped to the manifest total — the downloader's
        resume metadata can push raw staging size past it."""
        return sum(
            artifact.download_bytes
            if self._store.installed(artifact)
            else min(self._store.staged_bytes(artifact), artifact.download_bytes)
            for artifact in artifacts
        )


def _download_bytes(artifacts: tuple[PinnedArtifact, ...]) -> int:
    return sum(artifact.download_bytes for artifact in artifacts)
