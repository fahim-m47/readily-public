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
from readily_engine.store import DownloadManager, ModelStore


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
        "queue": [],
        "failures": [],
    }


def test_a_download_runs_to_installed(store: ModelStore):
    entry = make_entry(FILES)
    manager = DownloadManager(store, writing_fetch(FILES))

    manager.start(entry)

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
    manager.start(entry)
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
        manager.start(entry)
        assert manager.snapshot()["phase"] == "installed"
    finally:
        promoted.chmod(0o755)


def recording_fetch(fetched: list[str], gate: threading.Event):
    """A fetch that notes which model it fetched, then holds the download
    thread open until `gate` is set."""

    def fetch(entry: CatalogEntry, staging_dir: Path) -> None:
        fetched.append(entry.id)
        writing_fetch(FILES)(entry, staging_dir)
        assert gate.wait(timeout=5)

    return fetch


def test_downloads_asked_for_while_one_runs_wait_their_turn_in_click_order(
    store: ModelStore,
):
    kokoro = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    chatterbox = make_entry(FILES, name="chatterbox", tag="0.5b")
    fetched: list[str] = []
    gate = threading.Event()

    manager = DownloadManager(store, recording_fetch(fetched, gate))
    manager.start(kokoro)
    manager.start(chatterbox)
    manager.start(qwen)
    # The same model again is the same request, already accepted.
    manager.start(kokoro)
    manager.start(qwen)

    assert manager.snapshot()["modelId"] == kokoro.id
    assert manager.snapshot()["queue"] == [
        {"modelId": chatterbox.id, "action": "download"},
        {"modelId": qwen.id, "action": "download"},
    ]

    gate.set()
    snapshot = finished(manager)
    assert fetched == [kokoro.id, chatterbox.id, qwen.id]
    assert snapshot["phase"] == "installed"
    assert snapshot["modelId"] == qwen.id
    assert snapshot["queue"] == []
    assert all(store.installed(entry) for entry in (kokoro, qwen, chatterbox))


def test_a_delete_asked_for_while_a_download_runs_waits_its_turn(store: ModelStore):
    # Deleting is queued with downloads, not beside them: run beside one, a
    # delete can rmtree a live download's staging, or a download that was
    # already running can promote what was just deleted.
    kokoro = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    gate = threading.Event()

    manager = DownloadManager(store, blocking_fetch(gate))
    manager.start(kokoro)
    manager.start(qwen)

    assert manager.delete(kokoro) == "queued"
    assert manager.delete(kokoro) == "queued"
    assert manager.snapshot()["queue"] == [
        {"modelId": qwen.id, "action": "download"},
        {"modelId": kokoro.id, "action": "delete"},
    ]

    gate.set()
    snapshot = finished(manager)
    assert not store.installed(kokoro)
    assert store.installed(qwen)
    assert snapshot["modelId"] == qwen.id
    assert snapshot["queue"] == []


class SlowPurge(ModelStore):
    """The real store, holding the first rmtree of `slow` open until
    `released`, so a test can act while the worker is doing nothing but
    cleaning up. A later rmtree of it, from a request, runs at once."""

    def __init__(
        self, store: ModelStore, slow: CatalogEntry, released: threading.Event
    ) -> None:
        super().__init__(store.data_dir)
        self._slow = slow
        self._released = released
        self.purging = threading.Event()

    def purge(self, entry) -> None:
        if entry.id == self._slow.id and not self.purging.is_set():
            self.purging.set()
            assert self._released.wait(timeout=5)
        super().purge(entry)


def test_a_delete_asked_for_while_only_a_clean_up_runs_happens_at_once(
    store: ModelStore,
):
    # A delete waits for downloads, not for an earlier delete's rmtree.
    # Queued behind one, it would be retired the moment the rmtree ended,
    # and a reader polling the snapshot could see the queue empty before
    # and after without ever seeing it waiting — or the model gone.
    kokoro = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    chatterbox = make_entry(FILES, name="chatterbox", tag="0.5b")
    manager = DownloadManager(store, writing_fetch(FILES))
    manager.start(kokoro)
    manager.start(qwen)
    finished(manager)

    released = threading.Event()
    slow = SlowPurge(store, qwen, released)
    gate = threading.Event()
    manager = DownloadManager(slow, blocking_fetch(gate))
    manager.start(chatterbox)
    assert manager.delete(qwen) == "queued"
    gate.set()
    assert slow.purging.wait(timeout=5)

    assert manager.delete(kokoro) == "deleted"
    assert not store.installed(kokoro)
    assert manager.snapshot()["queue"] == []
    # The model being cleaned up is already gone, and nothing is waiting
    # that a `queued` answer could ever settle.
    assert manager.delete(qwen) == "absent"
    assert manager.snapshot()["queue"] == []

    # A download asked for meanwhile is still what a delete waits behind.
    manager.start(kokoro)
    assert manager.delete(kokoro) == "queued"
    assert manager.snapshot()["queue"] == [
        {"modelId": kokoro.id, "action": "download"},
        {"modelId": kokoro.id, "action": "delete"},
    ]

    released.set()
    snapshot = finished(manager)
    assert snapshot["failures"] == []
    assert snapshot["queue"] == []
    assert not store.installed(qwen)
    assert not store.installed(kokoro)


def test_a_waiting_job_can_be_withdrawn_before_it_starts(store: ModelStore):
    kokoro = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    chatterbox = make_entry(FILES, name="chatterbox", tag="0.5b")
    fetched: list[str] = []
    gate = threading.Event()

    manager = DownloadManager(store, recording_fetch(fetched, gate))
    manager.start(kokoro)
    manager.start(qwen)
    manager.start(chatterbox)

    assert manager.withdraw(qwen) is True
    assert manager.withdraw(qwen) is False
    # Only waiting jobs can be withdrawn; the running one carries on.
    assert manager.withdraw(kokoro) is False
    assert manager.snapshot()["queue"] == [
        {"modelId": chatterbox.id, "action": "download"},
    ]

    gate.set()
    finished(manager)
    assert fetched == [kokoro.id, chatterbox.id]
    assert store.installed(kokoro)
    assert not store.installed(qwen)


def test_a_delete_with_nothing_running_happens_at_once(store: ModelStore):
    entry = make_entry(FILES)
    manager = DownloadManager(store, writing_fetch(FILES))
    manager.start(entry)
    finished(manager)

    assert manager.delete(entry) == "deleted"
    assert not store.installed(entry)
    assert manager.delete(entry) == "absent"


def test_deleting_the_model_the_snapshot_describes_resets_it(store: ModelStore):
    # A snapshot left saying "installed" would keep every SSE reader
    # believing in a model that is no longer on disk.
    entry = make_entry(FILES)
    manager = DownloadManager(store, writing_fetch(FILES))
    manager.start(entry)
    assert finished(manager)["phase"] == "installed"

    assert manager.delete(entry) == "deleted"

    assert manager.snapshot() == {
        "version": 1,
        "phase": "idle",
        "modelId": None,
        "bytesTotal": 0,
        "bytesDownloaded": 0,
        "error": None,
        "queue": [],
        "failures": [],
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
        manager.start(entry)
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


def test_a_failure_stays_visible_after_the_queue_moves_on(store: ModelStore):
    # The top of the snapshot describes the newest download, so a failure
    # behind it would vanish before the reader saw it. The `failures` list
    # keeps it until the model is asked for again.
    broken = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    gate = threading.Event()
    attempts: list[str] = []

    def drops_the_first(entry: CatalogEntry, staging_dir: Path) -> None:
        assert gate.wait(timeout=5)
        attempts.append(entry.id)
        if attempts == [broken.id]:
            raise OSError("network dropped")
        writing_fetch(FILES)(entry, staging_dir)

    manager = DownloadManager(store, drops_the_first)
    manager.start(broken)
    manager.start(qwen)
    gate.set()

    snapshot = finished(manager)
    assert snapshot["phase"] == "installed"
    assert snapshot["modelId"] == qwen.id
    assert snapshot["failures"] == [
        {
            "modelId": broken.id,
            "error": {
                "version": 1,
                "code": "download_failed",
                "message": "The download could not be completed.",
            },
        }
    ]

    manager.start(broken)
    assert manager.snapshot()["failures"] == []
    assert finished(manager)["phase"] == "installed"


class SmallDisk(ModelStore):
    """The real store on a disk with only `free` bytes left."""

    def __init__(self, store: ModelStore, free: int) -> None:
        super().__init__(store.data_dir)
        self._free = free

    def free_bytes(self) -> int:
        return self._free


def test_a_waiting_download_that_no_longer_fits_is_skipped(store: ModelStore):
    kokoro = make_entry(FILES)
    big = make_entry(
        {**FILES, "model.onnx": b"x" * 10_000}, name="chatterbox", tag="0.5b"
    )
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    fetched: list[str] = []
    gate = threading.Event()

    manager = DownloadManager(
        SmallDisk(store, free=1_000), recording_fetch(fetched, gate)
    )
    manager.start(kokoro)
    manager.start(big)
    manager.start(qwen)
    gate.set()

    snapshot = finished(manager)
    assert fetched == [kokoro.id, qwen.id]
    assert snapshot["modelId"] == qwen.id
    assert snapshot["phase"] == "installed"
    assert snapshot["failures"] == [
        {
            "modelId": big.id,
            "error": {
                "version": 1,
                "code": "insufficient_disk_space",
                "message": "There is not enough free disk space to download "
                "this Voice Model.",
            },
        }
    ]


def test_a_download_whose_derived_graph_no_longer_fits_is_skipped(store: ModelStore):
    # Installing writes a copy of each derived file's source beside the
    # download (ADR 0011), so a disk with room for the download alone fills
    # at promotion, after every byte was fetched.
    entry = make_entry(
        FILES,
        derived_files=[
            {
                "path": "model.timed.onnx",
                "source": "model.onnx",
                "outputs": ["duration"],
            }
        ],
    )
    fetched: list[str] = []
    gate = threading.Event()
    gate.set()

    manager = DownloadManager(
        SmallDisk(store, free=entry.download_bytes), recording_fetch(fetched, gate)
    )
    manager.start(entry)

    snapshot = finished(manager)
    assert fetched == []
    assert snapshot["phase"] == "failed"
    assert snapshot["failures"] == [
        {
            "modelId": entry.id,
            "error": {
                "version": 1,
                "code": "insufficient_disk_space",
                "message": "There is not enough free disk space to download "
                "this Voice Model.",
            },
        }
    ]


class QuotaDisk(ModelStore):
    """The real store on a disk of `quota` bytes, so what the store frees
    is free again."""

    def __init__(self, store: ModelStore, quota: int) -> None:
        super().__init__(store.data_dir)
        self._quota = quota

    def free_bytes(self) -> int:
        return self._quota - tree_bytes(self.data_dir)


def tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def marked_broken(store: ModelStore, entry: CatalogEntry) -> Path:
    """Install `entry`, break it, and have repair mark it in a folder it
    could not write; then make the folder writable again, as the reader
    does before pressing Download. Returns the marked directory."""
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "voices" / "pack.bin").unlink()
    promoted.parent.chmod(0o500)
    try:
        store.repair([entry])
    finally:
        promoted.parent.chmod(0o755)
    assert not store.installed(entry)
    return promoted


def test_a_replacement_that_fits_once_its_broken_copy_is_reclaimed_is_downloaded(
    store: ModelStore,
):
    # The broken copy is nothing the reader can use or delete, and the
    # download itself is what frees it; the space check must not refuse
    # the download for the bytes it is about to reclaim.
    entry = make_entry(FILES)
    broken = marked_broken(store, entry)
    fetched: list[str] = []
    gate = threading.Event()
    gate.set()

    disk = QuotaDisk(store, quota=tree_bytes(broken) + entry.download_bytes - 1)
    manager = DownloadManager(disk, recording_fetch(fetched, gate))
    manager.start(entry)

    snapshot = finished(manager)
    assert fetched == [entry.id]
    assert snapshot["phase"] == "installed"
    assert snapshot["failures"] == []
    assert store.installed(entry)


def test_a_replacement_that_would_not_fit_even_then_is_still_skipped(
    store: ModelStore,
):
    entry = make_entry(FILES)
    broken = marked_broken(store, entry)
    fetched: list[str] = []
    gate = threading.Event()
    gate.set()

    disk = QuotaDisk(store, quota=entry.download_bytes - 1)
    manager = DownloadManager(disk, recording_fetch(fetched, gate))
    manager.start(entry)

    snapshot = finished(manager)
    assert fetched == []
    assert snapshot["phase"] == "failed"
    assert snapshot["failures"][0]["error"]["code"] == "insufficient_disk_space"
    # The copy was reclaimed either way: unusable, and not the reader's to
    # free from the Catalog, its bytes are the Engine's to give back.
    assert not broken.exists()
    assert not list(store.data_dir.rglob("*.deleting"))


class UnreadableDisk(ModelStore):
    """The real store on a disk whose free space cannot be read."""

    def free_bytes(self) -> int:
        raise OSError("statvfs failed")


def test_a_download_whose_disk_cannot_be_read_fails_and_the_queue_moves_on(
    store: ModelStore,
):
    kokoro = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")

    manager = DownloadManager(UnreadableDisk(store.data_dir), writing_fetch(FILES))
    manager.start(kokoro)
    manager.start(qwen)

    snapshot = finished(manager)
    assert [failure["modelId"] for failure in snapshot["failures"]] == [
        kokoro.id,
        qwen.id,
    ]
    assert snapshot["failures"][0]["error"]["code"] == "download_failed"
    # The worker stood down rather than dying holding the queue.
    manager.start(make_entry(FILES, name="chatterbox", tag="0.5b"))
    assert finished(manager)["queue"] == []


class StuckFolder(ModelStore):
    """The real store, unable to move `stuck`'s folder aside."""

    def __init__(self, store: ModelStore, stuck: CatalogEntry) -> None:
        super().__init__(store.data_dir)
        self._stuck = stuck

    def retire(self, entry) -> bool:
        if entry.id == self._stuck.id:
            raise PermissionError("read-only folder")
        return super().retire(entry)


def test_a_queued_delete_that_cannot_run_is_reported_and_does_not_stop_the_queue(
    store: ModelStore,
):
    kokoro = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    chatterbox = make_entry(FILES, name="chatterbox", tag="0.5b")
    gate = threading.Event()
    manager = DownloadManager(store, writing_fetch(FILES))
    manager.start(qwen)
    finished(manager)

    manager = DownloadManager(StuckFolder(store, qwen), blocking_fetch(gate))
    manager.start(kokoro)
    assert manager.delete(qwen) == "queued"
    manager.start(chatterbox)
    gate.set()

    snapshot = finished(manager)
    assert snapshot["modelId"] == chatterbox.id
    assert snapshot["phase"] == "installed"
    assert snapshot["queue"] == []
    assert store.installed(qwen)
    # Leaving the queue is how a reader learns a delete ran, so one that
    # did not has to say so.
    assert snapshot["failures"] == [
        {
            "modelId": qwen.id,
            "error": {
                "version": 1,
                "code": "store_unwritable",
                "message": "Readily cannot delete this Voice Model because the "
                f"folder {store.promoted_dir(qwen).parent} is not writable. Give "
                "Readily write access to it.",
            },
        }
    ]


class StuckThenFreed(SlowPurge):
    """`SlowPurge` whose first retire of the slow entry is refused: the
    folder the reader fixes while the worker is still cleaning up."""

    def __init__(
        self, store: ModelStore, slow: CatalogEntry, released: threading.Event
    ) -> None:
        super().__init__(store, slow, released)
        self._refused = False

    def retire(self, entry) -> bool:
        if entry.id == self._slow.id and not self._refused:
            self._refused = True
            raise PermissionError("read-only folder")
        return super().retire(entry)


def test_a_delete_asked_again_after_it_failed_runs_while_its_clean_up_does(
    store: ModelStore,
):
    # The reader fixes the folder and presses Delete again while the worker
    # is still on the failed delete's clean-up. That clean-up is not the
    # delete they asked for: answered `queued` with nothing waiting, the
    # model would stay installed and nothing would ever settle the request.
    kokoro = make_entry(FILES)
    qwen = make_entry(FILES, name="qwen3-tts", tag="0.6b")
    manager = DownloadManager(store, writing_fetch(FILES))
    manager.start(qwen)
    finished(manager)

    released = threading.Event()
    slow = StuckThenFreed(store, qwen, released)
    gate = threading.Event()
    manager = DownloadManager(slow, blocking_fetch(gate))
    manager.start(kokoro)
    assert manager.delete(qwen) == "queued"
    gate.set()
    assert slow.purging.wait(timeout=5)
    assert store.installed(qwen)
    assert manager.snapshot()["failures"][0]["error"]["code"] == "store_unwritable"

    assert manager.delete(qwen) == "deleted"
    assert not store.installed(qwen)
    assert manager.snapshot()["failures"] == []
    assert manager.snapshot()["queue"] == []

    released.set()
    snapshot = finished(manager)
    assert snapshot["queue"] == []
    assert not store.installed(qwen)
    assert not list(store.data_dir.rglob("*.deleting"))


def test_a_failed_download_can_be_retried(store: ModelStore):
    entry = make_entry(FILES)
    corrupted = {**FILES, "model.onnx": b"tampered bytes"}
    attempts: list[dict[str, bytes]] = [corrupted, FILES]

    def flaky(entry: CatalogEntry, staging_dir: Path) -> None:
        writing_fetch(attempts.pop(0))(entry, staging_dir)

    manager = DownloadManager(store, flaky)
    manager.start(entry)
    assert finished(manager)["phase"] == "failed"

    manager.start(entry)
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
