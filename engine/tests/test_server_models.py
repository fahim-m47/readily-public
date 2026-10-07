"""The model management routes: list, download, delete, progress SSE.

The store and download manager are faked at the app seam — their real
behavior is pinned by `test_store.py` and `test_model_downloads.py`; these
tests pin the wire contract in front of them.
"""

from collections.abc import AsyncIterator

from conftest import AUTH, TOKEN
from fastapi.testclient import TestClient

from readily_engine.catalog import CatalogEntry, load_manifest
from readily_engine.loading.architecture import Backend
from readily_engine.server.app import create_app
from readily_engine.store import DeleteOutcome

KOKORO = "kokoro:82m"
QWEN = "qwen3-tts:0.6b"

# What an Apple silicon Mac runs, pinned so these tests read the same on the
# ubuntu runner, which has no mlx-audio.
EVERY_BACKEND: frozenset[Backend] = frozenset({"onnxruntime", "mlx-audio"})


class FakeStore:
    def __init__(self, installed: set[str] | None = None) -> None:
        self.installed_ids = installed or set()
        self.usage = {model_id: 1000 for model_id in self.installed_ids}
        self.deleted: list[str] = []

    def installed(self, entry: CatalogEntry) -> bool:
        return entry.id in self.installed_ids

    def disk_usage(self, entry: CatalogEntry) -> int:
        return self.usage.get(entry.id, 0)

    def delete(self, entry: CatalogEntry) -> bool:
        self.deleted.append(entry.id)
        if entry.id not in self.installed_ids:
            return False
        self.installed_ids.discard(entry.id)
        return True


class FakeDownloads:
    """Deleting goes through the manager here as it does in production:
    it is the one place that can order a delete against a download."""

    def __init__(self, store: "FakeStore | None" = None, busy: bool = False) -> None:
        self.store = store if store is not None else FakeStore()
        # Whether a job is running, so a delete has to wait its turn.
        self.busy = busy
        self.started: list[str] = []

    def start(self, entry: CatalogEntry, *, support=()) -> None:
        self.started.append(entry.id)

    def delete(self, entry: CatalogEntry) -> DeleteOutcome:
        if self.busy:
            return "queued"
        return "deleted" if self.store.delete(entry) else "absent"

    def withdraw(self, entry: CatalogEntry) -> bool:
        if entry.id not in self.started:
            return False
        self.started.remove(entry.id)
        return True

    async def events(self) -> AsyncIterator[dict[str, object]]:
        yield {
            "version": 1,
            "phase": "downloading",
            "modelId": KOKORO,
            "bytesTotal": 10,
            "bytesDownloaded": 5,
            "error": None,
        }


def client(
    store: FakeStore | None = None,
    downloads: FakeDownloads | None = None,
    backends: frozenset[Backend] = EVERY_BACKEND,
) -> TestClient:
    store = store if store is not None else FakeStore()
    return TestClient(
        create_app(
            token=TOKEN,
            store=store,
            downloads=downloads if downloads is not None else FakeDownloads(store),
            backends=backends,
        )
    )


def test_the_model_routes_need_the_launch_token():
    unauthenticated = client()
    assert unauthenticated.get("/v1/models").status_code == 401
    assert unauthenticated.post(f"/v1/models/{KOKORO}/download").status_code == 401
    assert unauthenticated.delete(f"/v1/models/{KOKORO}").status_code == 401
    assert unauthenticated.get("/v1/models/events").status_code == 401


def test_the_listing_reports_every_entry_with_install_state_and_disk_usage():
    response = client(FakeStore(installed={KOKORO})).get("/v1/models", headers=AUTH)

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == 1
    assert [model["id"] for model in payload["models"]] == [
        entry.id for entry in load_manifest().models
    ]
    by_id = {model["id"]: model for model in payload["models"]}
    assert by_id[KOKORO]["installed"] is True
    assert by_id[KOKORO]["diskBytes"] == 1000
    assert by_id[QWEN]["installed"] is False
    assert by_id[QWEN]["diskBytes"] == 0
    assert by_id[QWEN]["downloadBytes"] > 0
    assert payload["diskBytesTotal"] == 1000


def test_a_download_is_accepted_and_handed_to_the_manager():
    downloads = FakeDownloads()
    response = client(downloads=downloads).post(
        f"/v1/models/{QWEN}/download", headers=AUTH
    )

    assert response.status_code == 202
    assert response.json() == {"version": 1, "modelId": QWEN, "status": "accepted"}
    assert downloads.started == [QWEN]


def test_a_bare_name_resolves_to_the_default_tag():
    downloads = FakeDownloads()
    response = client(downloads=downloads).post(
        "/v1/models/kokoro/download", headers=AUTH
    )

    assert response.status_code == 202
    assert response.json()["modelId"] == KOKORO
    assert downloads.started == [KOKORO]


def test_a_model_outside_the_catalog_cannot_be_downloaded():
    downloads = FakeDownloads()
    response = client(downloads=downloads).post(
        "/v1/models/not-a-model/download", headers=AUTH
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_model"
    assert downloads.started == []


def test_a_model_this_machine_cannot_run_is_refused_before_it_downloads():
    # An Intel Mac or Linux build has no MLX lane, so an expressive model
    # would download gigabytes it can never load.
    downloads = FakeDownloads()
    response = client(downloads=downloads, backends=frozenset({"onnxruntime"})).post(
        f"/v1/models/{QWEN}/download", headers=AUTH
    )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "version": 1,
        "code": "model_unsupported",
        "message": "This Voice Model cannot run on this computer.",
    }
    assert downloads.started == []


def test_delete_removes_an_installed_model():
    store = FakeStore(installed={KOKORO})
    response = client(store).delete(f"/v1/models/{KOKORO}", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "version": 1,
        "modelId": KOKORO,
        "deleted": True,
        "queued": False,
    }
    assert store.deleted == [KOKORO]


def test_delete_of_an_absent_model_reports_nothing_deleted():
    response = client().delete(f"/v1/models/{KOKORO}", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["deleted"] is False


def test_delete_of_an_unknown_model_is_refused():
    response = client().delete("/v1/models/not-a-model", headers=AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_model"


def test_a_delete_asked_for_while_a_job_runs_waits_its_turn():
    store = FakeStore(installed={KOKORO})
    response = client(store, FakeDownloads(store, busy=True)).delete(
        f"/v1/models/{KOKORO}", headers=AUTH
    )

    assert response.status_code == 202
    assert response.json() == {
        "version": 1,
        "modelId": KOKORO,
        "deleted": False,
        "queued": True,
    }
    assert store.deleted == []


def test_a_waiting_job_can_be_withdrawn():
    downloads = FakeDownloads()
    api = client(downloads=downloads)
    api.post(f"/v1/models/{QWEN}/download", headers=AUTH)

    first = api.delete(f"/v1/models/{QWEN}/queue", headers=AUTH)
    again = api.delete(f"/v1/models/{QWEN}/queue", headers=AUTH)

    assert first.status_code == 200
    assert first.json() == {"version": 1, "modelId": QWEN, "removed": True}
    assert again.json()["removed"] is False
    assert api.delete("/v1/models/not-a-model/queue", headers=AUTH).status_code == 404


def test_download_progress_streams_as_download_events():
    response = client().get("/v1/models/events", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("retry: 1000\n\n")
    assert "event: download\n" in response.text
    assert "id: 1\n" in response.text
    assert 'data: {"version":1,"phase":"downloading"' in response.text


def test_speech_refuses_a_model_that_is_not_installed():
    response = client(FakeStore()).post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"model": KOKORO, "input": "hello", "voice": "af_heart"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "version": 1,
        "code": "model_not_installed",
        "message": "This Voice Model has not been downloaded.",
    }


def test_speech_refuses_a_model_this_machine_cannot_run_whether_or_not_it_is_on_disk():
    # The Apple-silicon build can fill a data directory the Intel build then
    # opens, so an expressive model can be on disk where nothing can load
    # it. Refusing only the download would let narrating fail importing
    # MLX on the event stream instead.
    for store in (FakeStore(installed={QWEN}), FakeStore()):
        response = client(store, backends=frozenset({"onnxruntime"})).post(
            "/v1/audio/speech",
            headers=AUTH,
            json={"model": QWEN, "input": "hello", "voice": "Chelsie"},
        )

        assert response.status_code == 409, store.installed_ids
        assert response.json()["error"]["code"] == "model_unsupported"


def test_a_model_this_machine_cannot_run_can_still_be_deleted():
    store = FakeStore(installed={QWEN})
    response = client(store, backends=frozenset({"onnxruntime"})).delete(
        f"/v1/models/{QWEN}", headers=AUTH
    )

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert store.deleted == [QWEN]


def test_fallback_entries_are_ready_without_the_support_model_by_default():
    rows = {
        row["id"]: row
        for row in client(FakeStore(installed={KOKORO, QWEN}))
        .get("/v1/models", headers=AUTH)
        .json()["models"]
    }
    assert rows[KOKORO]["installed"] is True
    assert rows[QWEN]["installed"] is True
