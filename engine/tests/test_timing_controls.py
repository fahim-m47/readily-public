"""Timing controls select a curated fallback and freeze it with the Block."""

import json

import pytest

from readily_engine.catalog import Manifest, load_manifest
from readily_engine.generation import GenerationRecord


def test_the_aligner_is_off_by_default_and_choosing_it_changes_the_record():
    entry = load_manifest().find("qwen3-tts:0.6b")
    assert entry.control_values["word_timing"] == "off"
    original = GenerationRecord.for_entry(entry, entry.default_voice, "Hello.")
    aligned = GenerationRecord.for_entry(
        entry.compose({"word_timing": "wav2vec2:base-960h"}).entry,
        entry.default_voice,
        "Hello.",
    )
    assert original.word_timing == "off"
    assert aligned.word_timing == "wav2vec2:base-960h"
    assert original.key != aligned.key
    assert "word_timing" not in json.loads(original.canonical_json())
    assert GenerationRecord.from_json(original.canonical_json()).key == original.key
    assert GenerationRecord.from_json(aligned.canonical_json()) == aligned
    with pytest.raises(ValueError):
        entry.compose({"word_timing": "unreviewed:model"})


def test_timing_choices_cannot_name_an_uncurated_support_model():
    document = load_manifest().model_dump(mode="json")
    entry = next(e for e in document["models"] if e["name"] == "qwen3-tts")
    entry["parameters"]["word_timing"]["choices"].append("unreviewed:model")
    with pytest.raises(ValueError, match="curated Support Model"):
        Manifest.model_validate(document)


def test_only_entries_without_native_times_can_need_the_support_model():
    catalog = load_manifest()
    for entry in catalog.models:
        assert catalog.required_artifacts(entry, {entry.word_timing}) == (entry,)
        if entry.name in {"qwen3-tts", "chatterbox", "supertonic"}:
            chosen = entry.compose({"word_timing": "wav2vec2:base-960h"}).entry
            choice = chosen.timing_choice(chosen.default_voice)
            assert len(catalog.required_artifacts(chosen, {choice})) == 2


def test_choosing_the_aligner_adds_support_to_download_and_readiness(tmp_path):
    from conftest import AUTH, TOKEN
    from fastapi.testclient import TestClient
    from storage_fakes import open_test_storage
    from test_server_models import FakeDownloads, FakeStore

    from readily_engine.server.app import create_app

    catalog = load_manifest()
    entry = catalog.find("qwen3-tts:0.6b")
    (aligner,) = catalog.required_support({"wav2vec2:base-960h"})
    storage = open_test_storage(tmp_path)
    storage.set_control_overrides(
        entry, entry.default_voice, {"word_timing": aligner.id}
    )
    starts = []

    class Downloads(FakeDownloads):
        def start(self, entry, *, support=()):
            starts.append((entry, support))
            return True

    api = TestClient(
        create_app(
            token=TOKEN,
            history=storage,
            store=FakeStore({entry.id}),
            downloads=Downloads(),
        )
    )
    try:
        rows = api.get("/v1/models", headers=AUTH).json()["models"]
        row = next(row for row in rows if row["id"] == entry.id)
        assert row["installed"] is False
        assert row["downloadBytes"] == entry.download_bytes + aligner.download_bytes
        response = api.post(f"/v1/models/{entry.id}/download", headers=AUTH)
        assert response.status_code == 202
        assert starts == [(entry, (aligner,))]
    finally:
        storage.close()


@pytest.mark.parametrize("operation", ["resume", "export"])
@pytest.mark.parametrize(
    "timing,cached,status",
    [
        ("off", False, 202),
        ("wav2vec2:base-960h", False, 409),
        ("wav2vec2:base-960h", True, 202),
    ],
)
def test_saved_readiness_uses_frozen_block_selection(
    tmp_path, operation, timing, cached, status
):
    from dataclasses import replace

    from conftest import AUTH
    from test_server_models import FakeStore
    from test_server_wire import RecordingHistory, history_client

    entry = load_manifest().find("qwen3-tts:0.6b")
    record = GenerationRecord.for_entry(
        entry.compose({"word_timing": timing}).entry,
        entry.default_voice,
        "Read locally.",
    )
    history = RecordingHistory()
    history.item = replace(
        history.item,
        model_id=entry.id,
        voice_id=entry.default_voice,
        segments=(
            replace(
                history.item.segments[0],
                audio_present=cached,
                word_timing=record.word_timing,
            ),
        ),
    )
    client = history_client(history, store=FakeStore({entry.id}))
    payload = (
        {"destination": str(tmp_path / "frozen.wav"), "format": "wav"}
        if operation == "export"
        else None
    )
    response = client.post(f"/v1/history/n-1/{operation}", headers=AUTH, json=payload)
    assert response.status_code == status
