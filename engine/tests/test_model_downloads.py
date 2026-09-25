"""The download manager: one download at a time, snapshot state for SSE.

Drives the real `ModelStore` under a temp data dir with fake fetches, so
phases, failure codes, and the busy policy are exercised end to end with
no network.
"""

import asyncio
import threading
from pathlib import Path

import pytest
from conftest import FILES, make_entry, wait_until, writing_fetch

from readily_engine.catalog import CatalogEntry
from readily_engine.store import DownloadInProgress, DownloadManager, ModelStore


@pytest.fixture
def store(tmp_path: Path) -> ModelStore:
    return ModelStore(tmp_path)


def finished(manager: DownloadManager) -> dict[str, object]:
    assert manager.thread is not None
    manager.thread.join(timeout=5)
    assert not manager.thread.is_alive()
    return manager.snapshot()


def blocking_fetch(gate: threading.Event):
    """A fetch that stages everything, then holds the download thread open."""

    def fetch(entry: CatalogEntry, staging_dir: Path) -> None:
        writing_fetch(FILES)(entry, staging_dir)
        assert gate.wait(timeout=5)

    return fetch


class HoldingStore(ModelStore):
    """The real store, held at the verifying phase so a test can read the
    snapshot from the window between download and promotion."""

    def __init__(self, store: ModelStore, at_verifying: threading.Event) -> None:
        super().__init__(store.data_dir)
        self._at_verifying = at_verifying

    def install(self, entry, fetch, on_phase=lambda phase: None) -> None:
        def hold(phase: str) -> None:
            on_phase(phase)
            if phase == "verifying":
                assert self._at_verifying.wait(timeout=5)

        super().install(entry, fetch, on_phase=hold)


def test_idle_until_asked(store: ModelStore):
    manager = DownloadManager(store, writing_fetch(FILES))

    assert manager.snapshot() == {
        "version": 1,
        "phase": "idle",
        "modelId": None,
        "bytesTotal": 0,
        "bytesDownloaded": 0,
        "error": None,
    }


def test_a_download_runs_to_installed(store: ModelStore):
    entry = make_entry(FILES)
    manager = DownloadManager(store, writing_fetch(FILES))

    assert manager.start(entry) is True

    snapshot = finished(manager)
    assert snapshot["phase"] == "installed"
    assert snapshot["modelId"] == entry.id
    assert snapshot["bytesTotal"] == entry.download_bytes
    assert snapshot["bytesDownloaded"] == entry.download_bytes
    assert snapshot["error"] is None
    assert store.installed(entry)


def test_an_installed_model_is_reported_without_refetching(store: ModelStore):
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))

    def never(entry: CatalogEntry, staging_dir: Path) -> None:
        raise AssertionError("an installed model must never be re-fetched")

    manager = DownloadManager(store, never)
    assert manager.start(entry) is True
    assert manager.snapshot()["phase"] == "installed"


def test_an_installed_model_stays_usable_when_its_licence_cannot_be_written(
    store: ModelStore,
):
    # Writing the licence in is a favour to an older install, not a
    # condition of it: a full or read-only disk must not turn "installed"
    # into an error on the request that asked for the model.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "LICENSE").unlink()
    promoted.chmod(0o555)

    def never(entry: CatalogEntry, staging_dir: Path) -> None:
        raise AssertionError("an installed model must never be re-fetched")

    try:
        manager = DownloadManager(store, never)
        assert manager.start(entry) is True
        assert manager.snapshot()["phase"] == "installed"
    finally:
        promoted.chmod(0o755)


def test_one_download_at_a_time(store: ModelStore):
    kokoro = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    gate = threading.Event()

    manager = DownloadManager(store, blocking_fetch(gate))
    assert manager.start(kokoro) is True
    # The same model again is the same download, already accepted.
    assert manager.start(kokoro) is True
    assert manager.start(qwen) is False

    gate.set()
    assert finished(manager)["phase"] == "installed"
    assert manager.start(qwen) is True


def test_a_model_cannot_be_deleted_out_from_under_its_own_download(store: ModelStore):
    # Asking "is it busy?" and deleting are one step under the manager's
    # lock: split in two, a delete can rmtree a live download's staging, or
    # a download that was already running can promote what was just deleted.
    kokoro = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    gate = threading.Event()

    manager = DownloadManager(store, blocking_fetch(gate))
    manager.start(kokoro)

    with pytest.raises(DownloadInProgress):
        manager.delete(kokoro)
    assert manager.delete(qwen) is False

    gate.set()
    assert finished(manager)["phase"] == "installed"
    assert manager.delete(kokoro) is True
    assert not store.installed(kokoro)


def test_deleting_the_model_the_snapshot_describes_resets_it(store: ModelStore):
    # A snapshot left saying "installed" would keep every SSE reader
    # believing in a model that is no longer on disk.
    entry = make_entry(FILES)
    manager = DownloadManager(store, writing_fetch(FILES))
    manager.start(entry)
    assert finished(manager)["phase"] == "installed"

    assert manager.delete(entry) is True

    assert manager.snapshot() == {
        "version": 1,
        "phase": "idle",
        "modelId": None,
        "bytesTotal": 0,
        "bytesDownloaded": 0,
        "error": None,
    }


def test_progress_does_not_fall_back_to_zero_while_verifying(store: ModelStore):
    # Live byte progress is patched into the snapshots `events` yields only
    # while downloading. If the phase change did not record the bytes it had,
    # the whole verifying window would report the bytes staging held when the
    # download began — zero, for a fresh multi-gigabyte model.
    entry = make_entry(FILES)
    verifying = threading.Event()
    manager = DownloadManager(HoldingStore(store, verifying), writing_fetch(FILES))
    manager.start(entry)
    wait_until(lambda: manager.snapshot()["phase"] == "verifying", timeout=5.0)

    assert manager.snapshot()["bytesDownloaded"] == entry.download_bytes

    verifying.set()
    assert finished(manager)["phase"] == "installed"


def test_a_fetch_failure_reports_download_failed(store: ModelStore):
    entry = make_entry(FILES)

    def dropped(entry: CatalogEntry, staging_dir: Path) -> None:
        raise OSError("network dropped")

    manager = DownloadManager(store, dropped)
    manager.start(entry)

    snapshot = finished(manager)
    assert snapshot["phase"] == "failed"
    assert snapshot["error"] == {
        "version": 1,
        "code": "download_failed",
        "message": "The download could not be completed.",
    }
    assert not store.installed(entry)


def test_a_broken_model_the_store_cannot_replace_names_the_folder(store: ModelStore):
    # Repair marked a broken model whose folder it could not write.
    # A download would fail at promotion for the same reason, so the
    # manager refuses before fetching a byte, and the message names the
    # folder the reader has to fix.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "voices" / "pack.bin").unlink()
    promoted.parent.chmod(0o500)
    try:
        store.repair([entry])

        def never(entry: CatalogEntry, staging_dir: Path) -> None:
            raise AssertionError("a model that cannot be promoted must not be fetched")

        manager = DownloadManager(store, never)
        assert manager.start(entry) is True
        snapshot = finished(manager)
    finally:
        promoted.parent.chmod(0o755)

    assert snapshot["phase"] == "failed"
    error = snapshot["error"]
    assert isinstance(error, dict)
    assert error["code"] == "store_unwritable"
    assert str(promoted.parent) in str(error["message"])
    assert "write" in str(error["message"])


def test_a_corrupted_download_reports_verification_failed(store: ModelStore):
    entry = make_entry(FILES)
    corrupted = {**FILES, "model.onnx": b"tampered bytes"}

    manager = DownloadManager(store, writing_fetch(corrupted))
    manager.start(entry)

    snapshot = finished(manager)
    assert snapshot["phase"] == "failed"
    assert snapshot["error"] == {
        "version": 1,
        "code": "verification_failed",
        "message": "The downloaded files failed verification.",
    }
    assert not store.installed(entry)


def test_a_failed_download_can_be_retried(store: ModelStore):
    entry = make_entry(FILES)
    corrupted = {**FILES, "model.onnx": b"tampered bytes"}
    attempts: list[dict[str, bytes]] = [corrupted, FILES]

    def flaky(entry: CatalogEntry, staging_dir: Path) -> None:
        writing_fetch(attempts.pop(0))(entry, staging_dir)

    manager = DownloadManager(store, flaky)
    manager.start(entry)
    assert finished(manager)["phase"] == "failed"

    assert manager.start(entry) is True
    assert finished(manager)["phase"] == "installed"
    assert store.installed(entry)


def test_events_yield_complete_snapshots(store: ModelStore):
    manager = DownloadManager(store, writing_fetch(FILES))

    async def first_event() -> dict[str, object]:
        stream = manager.events()
        event = await anext(stream)
        await stream.aclose()
        return event

    assert asyncio.run(first_event()) == manager.snapshot()
