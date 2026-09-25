"""One download at a time, observable as SSE snapshots.

Publishes through `readily_engine.wire.Observable`, the same helper the
generation worker publishes through. The store owns the verify-promote
invariant; this class only sequences it on a background thread and
translates its outcomes into wire phases.
"""

import logging
import threading
from collections.abc import AsyncIterator
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


class DownloadSnapshot(TypedDict):
    """The frozen v1 download snapshot (`docs/wire.md`), as sent."""

    version: int
    phase: DownloadPhase
    modelId: str | None
    bytesTotal: int
    bytesDownloaded: int
    error: WireError | None


class DownloadInProgress(Exception):
    """The requested change conflicts with the download already running."""


class DownloadManager:
    """Run `store.install` for one entry at a time on a background thread.

    Also the serialization point for *deleting* a model: starting a
    download and deleting one are the two writers of a model's directories,
    so both happen under this class's one lock. Checking "is this model
    busy?" and acting on the answer in two steps is what lets a delete
    rmtree a live download's staging, or a still-running download promote a
    model the user just deleted.
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
            ),
            poll_seconds=_POLL_SECONDS,
        )
        self._lock = self._state.lock
        self._active: CatalogEntry | None = None
        self._artifacts: tuple[PinnedArtifact, ...] = ()
        self.thread: threading.Thread | None = None

    def start(
        self, entry: CatalogEntry, *, support: tuple[SupportModel, ...] = ()
    ) -> bool:
        """Begin (or resume) downloading `entry` and the `support` it needs,
        support first. Returns False only when a different model's download
        is in flight."""
        artifacts: tuple[PinnedArtifact, ...] = (*support, entry)
        with self._lock:
            if self.thread is not None and self.thread.is_alive():
                assert self._active is not None
                return self._active.id == entry.id
            if all(self._store.installed(artifact) for artifact in artifacts):
                self._state.update(
                    phase="installed",
                    modelId=entry.id,
                    bytesTotal=_download_bytes(artifacts),
                    bytesDownloaded=_download_bytes(artifacts),
                    error=None,
                )
                return True
            self._active = entry
            self._artifacts = artifacts
            self._state.update(
                phase="downloading",
                modelId=entry.id,
                bytesTotal=_download_bytes(artifacts),
                bytesDownloaded=self._progress_bytes(artifacts),
                error=None,
            )
            self.thread = threading.Thread(
                target=self._run,
                args=(entry, artifacts),
                daemon=True,
                name="readily-download",
            )
            self.thread.start()
            return True

    def delete(self, entry: CatalogEntry) -> bool:
        """Delete `entry`'s installed files, or raise `DownloadInProgress`
        when its own download is running. The busy check and the retire
        happen under the same lock that starts downloads, so no download
        can begin between them; the slow rmtree runs after the lock is
        released. Returns whether a promoted model existed."""
        with self._lock:
            if self._busy_with(entry):
                raise DownloadInProgress(entry.id)
            # Only the rename happens under the lock. `Observable.events`
            # polls this same lock from the asyncio loop, so an rmtree of a
            # multi-GB model here would freeze every route — /health
            # included — long enough for the supervisor to restart a
            # healthy Engine mid-delete.
            deleted = self._store.retire(entry)
            if deleted and self._state.snapshot()["modelId"] == entry.id:
                self._state.update(
                    phase="idle",
                    modelId=None,
                    bytesTotal=0,
                    bytesDownloaded=0,
                    error=None,
                )
        self._store.purge(entry)
        return deleted

    def _busy_with(self, entry: CatalogEntry) -> bool:
        """Whether `entry`'s download thread is running. Call under `_lock`."""
        return (
            self.thread is not None
            and self.thread.is_alive()
            and self._active is not None
            and self._active.id == entry.id
        )

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
            artifacts = self._artifacts if self._active is not None else ()
        if snapshot["phase"] != "downloading" or not artifacts:
            return snapshot
        return {**snapshot, "bytesDownloaded": self._progress_bytes(artifacts)}

    def _run(self, entry: CatalogEntry, artifacts: tuple[PinnedArtifact, ...]) -> None:
        try:
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
        self._state.update(
            phase="failed",
            bytesDownloaded=self._progress_bytes(self._artifacts),
            error=wire_error(code, message),
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
