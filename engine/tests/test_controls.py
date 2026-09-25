"""Only declared controls can change a Narration's frozen inputs."""

from types import SimpleNamespace

import pytest
from conftest import AUTH, TOKEN, wait_until
from fastapi.testclient import TestClient
from storage_fakes import open_test_storage
from worker_fakes import RecordingPlayback, RecordingSynthesizer, voice_model

from readily_engine.catalog import CatalogEntry, load_manifest
from readily_engine.chunking import chunk
from readily_engine.generation import GenerationRecord
from readily_engine.loading import qwen3
from readily_engine.narration.worker import GenerationWorker, NarrationRequest
from readily_engine.server.app import create_app
from readily_engine.storage.history import (
    HistoryStore,
    NarrationStatus,
    SynthesisSettings,
)

CONTROLS_PATH = "/v1/settings/controls"
SUPERTONIC = {"modelId": "supertonic:66m", "voiceId": "M1"}


def test_prepare_first_reaches_a_new_narration_through_the_controls_route(tmp_path):
    """Preparation is a Control like any other, saved per Voice.

    A stale override the current entry no longer admits must not stand in
    the way: it is dropped on read, and the Voice keeps the preparation the
    Reader asked for.
    """
    entry = supertonic()
    previous = entry.model_copy(
        update={
            "tunables": entry.tunables.model_copy(update={"chunk_budget_chars": 500})
        }
    )
    storage = open_test_storage(tmp_path)
    storage.set_control_overrides(previous, "M1", {"first_block_chars": 400})
    client = TestClient(create_app(token=TOKEN, history=storage))
    worker = GenerationWorker(
        {entry.id: voice_model(RecordingSynthesizer(), entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice="M1",
    )
    try:
        body = {**SUPERTONIC, "overrides": {"prepare_first": True}}
        assert client.patch(CONTROLS_PATH, json=body).status_code == 401
        response = client.patch(CONTROLS_PATH, headers=AUTH, json=body)
        assert response.status_code == 200
        assert response.json()["effectiveValues"]["prepare_first"] is True
        assert storage.effective_controls(entry, "F1").prepare_first is True
        narration_id = worker.start(
            NarrationRequest(model=entry.id, voice="M1", input="Fresh settings.")
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert storage.plan(narration_id).settings.prepare_first is True
    finally:
        worker.close()
        storage.close()


def supertonic() -> CatalogEntry:
    return load_manifest().find("supertonic:66m")


def settings_for(entry: CatalogEntry) -> SynthesisSettings:
    return SynthesisSettings(
        entry.id,
        entry.version,
        entry.default_voice,
        1.0,
        entry.tunables.pause_policy,
    )


def test_a_steps_override_changes_the_generation_record():
    entry = supertonic()
    original = GenerationRecord.for_entry(entry, entry.default_voice, "Hello.")
    changed = GenerationRecord.for_entry(
        entry.compose({"steps": 12}).entry, entry.default_voice, "Hello."
    )
    assert changed.parameters["steps"] == 12
    assert original.parameters["steps"] == 8
    assert changed.key != original.key
    assert GenerationRecord.from_json(changed.canonical_json()) == changed


@pytest.mark.parametrize(
    "overrides",
    [
        {"speed": 2},
        {"steps": 0},
        {"steps": 1.5},
        {"steps": None},
        {"steps": float("nan")},
        {"steps": float("inf")},
        {"seed": -1},
        {"seed": 2**32},
        {"seed": True},
        {"prepare_first": 1},
        {"pause_sentence_ms": -1},
        {"first_block_chars": 400, "chunk_budget_chars": 200},
    ],
)
def test_undeclared_or_out_of_range_overrides_are_refused(overrides):
    with pytest.raises(ValueError):
        supertonic().compose(overrides)


def test_an_entry_default_outside_its_declared_range_is_refused():
    named = supertonic().model_dump(mode="json")
    named["parameters"]["steps"]["hard_range"]["min"] = 12
    with pytest.raises(ValueError):
        CatalogEntry.model_validate(named)


def test_declaring_a_parameter_the_entry_does_not_set_is_refused():
    named = supertonic().model_dump(mode="json")
    named["parameters"]["top_p"] = {
        "label": "Top P",
        "unit": "probability",
        "description": "Nucleus sampling this entry never passes.",
        "kind": "number",
        "hard_range": {"min": 0.0, "max": 1.0, "min_exclusive": True},
    }
    with pytest.raises(ValueError):
        CatalogEntry.model_validate(named)


def test_a_decode_mode_the_engine_cannot_run_is_refused():
    named = load_manifest().find("qwen3-tts:0.6b").model_dump(mode="json")
    named["parameters"]["decode_mode"]["choices"].append("turbo")
    with pytest.raises(ValueError, match="decode_mode"):
        CatalogEntry.model_validate(named)


def test_overrides_persist_per_entry_and_voice_and_reset_to_defaults(tmp_path):
    entry = supertonic()
    kokoro = load_manifest().find("kokoro:82m")
    storage = open_test_storage(tmp_path)
    try:
        storage.set_control_overrides(entry, entry.default_voice, {"steps": 12})
    finally:
        storage.close()
    storage = open_test_storage(tmp_path)
    try:
        composed = storage.effective_controls(entry, entry.default_voice)
        assert composed.entry.generation_parameters["steps"] == 12
        other = storage.effective_controls(entry, entry.voices[1].id)
        assert other.entry.generation_parameters["steps"] == 8
        assert storage.effective_controls(kokoro, "af_heart").values == (
            kokoro.compose({}).values
        )
        with pytest.raises(ValueError):
            storage.set_control_overrides(
                entry, entry.default_voice, {"temperature": 0.5}
            )
        assert (
            storage.effective_controls(
                entry, entry.default_voice
            ).entry.generation_parameters["steps"]
            == 12
        )
        storage.set_control_overrides(entry, entry.default_voice, {})
        assert (
            storage.effective_controls(
                entry, entry.default_voice
            ).entry.generation_parameters["steps"]
            == 8
        )
    finally:
        storage.close()


def test_stale_overrides_are_dropped_on_read(tmp_path):
    entry = supertonic()
    history = HistoryStore.open(tmp_path / "readily.db")
    try:
        history.set_control_overrides(entry.id, "M1", {"steps": 12, "unknown": 1})
    finally:
        history.close()
    narrowed = entry.model_copy(
        update={
            "parameters": {
                "steps": entry.parameters["steps"].model_copy(
                    update={
                        "hard_range": entry.parameters["steps"].hard_range.model_copy(
                            update={"max": 8}
                        )
                    }
                )
            }
        }
    )
    storage = open_test_storage(tmp_path)
    client = TestClient(create_app(token=TOKEN, history=storage))
    try:
        composed = storage.effective_controls(narrowed, "M1")
        assert composed.entry.generation_parameters["steps"] == 8
        catalog = client.get("/v1/catalog", headers=AUTH)
        assert catalog.status_code == 200
        model = next(
            item for item in catalog.json()["models"] if item["id"] == entry.id
        )
        assert model["effectiveValues"]["M1"]["steps"] == 12
    finally:
        storage.close()


def test_new_narrations_freeze_controls_and_saved_narrations_keep_them(tmp_path):
    entry = supertonic()
    storage = open_test_storage(tmp_path)
    storage.set_control_overrides(
        entry,
        entry.default_voice,
        {
            "steps": 12,
            "seed": 42,
            "pause_sentence_ms": 123,
            "chunk_budget_chars": 30,
            "first_block_chars": 20,
        },
    )
    worker = GenerationWorker(
        {entry.id: voice_model(RecordingSynthesizer(), entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        narration_id = worker.start(
            NarrationRequest(
                model=entry.id,
                voice=entry.default_voice,
                input="First sentence. Second sentence. Third sentence.",
            )
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        plan = storage.plan(narration_id)
        assert len(plan.segments) == 3
        assert plan.settings.pause_policy.pause_sentence_ms == 123
        assert all(part.generation.parameters["steps"] == 12 for part in plan.segments)
        assert all(part.generation.seed == 42 for part in plan.segments)
        storage.set_control_overrides(entry, entry.default_voice, {"steps": 5})
        worker.resume(narration_id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert storage.plan(narration_id).segments == plan.segments
    finally:
        worker.close()
        storage.close()


def test_catalog_refresh_recomposes_the_voices_current_overrides(tmp_path):
    entry = supertonic()
    storage = open_test_storage(tmp_path)
    controls = storage.set_control_overrides(
        entry, entry.default_voice, {"steps": 12, "seed": 42}
    )
    plan = storage.create(
        chunk("Saved choices.", entry.tunables),
        settings_for(entry),
        controls.entry,
        seed=controls.seed,
    )
    storage.set_status(plan.id, NarrationStatus.STOPPED, playhead_sec=0.0)
    updated = entry.model_copy(update={"version": entry.version + 1})
    synth = RecordingSynthesizer()
    worker = GenerationWorker(
        {entry.id: voice_model(synth, updated)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        worker.resume(plan.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        record = storage.plan(plan.id).segments[0].generation
        assert record.catalog_version == updated.version
        assert record.key != plan.segments[0].key
        assert record.parameters["steps"] == 12
        assert record.seed == 42
        assert synth.inputs == ["Saved choices."]
    finally:
        worker.close()
        storage.close()


def test_catalog_refresh_takes_the_new_default_when_nothing_is_overridden(tmp_path):
    entry = supertonic()
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("Fresh defaults.", entry.tunables), settings_for(entry), entry
    )
    storage.set_status(plan.id, NarrationStatus.STOPPED, playhead_sec=0.0)
    updated = entry.model_copy(
        update={
            "version": entry.version + 1,
            "generation_parameters": entry.generation_parameters | {"steps": 6},
        }
    )
    worker = GenerationWorker(
        {entry.id: voice_model(RecordingSynthesizer(), updated)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        worker.resume(plan.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        record = storage.plan(plan.id).segments[0].generation
        assert record.parameters["steps"] == 6
        assert record.seed is None
    finally:
        worker.close()
        storage.close()


def test_the_controls_route_needs_the_launch_token(tmp_path):
    storage = open_test_storage(tmp_path)
    client = TestClient(create_app(token=TOKEN, history=storage))
    try:
        response = client.patch(CONTROLS_PATH, json={**SUPERTONIC, "overrides": {}})
        assert response.status_code == 401
    finally:
        storage.close()


def test_a_stored_override_projects_into_the_picker(tmp_path):
    storage = open_test_storage(tmp_path)
    client = TestClient(create_app(token=TOKEN, history=storage))
    try:
        response = client.patch(
            CONTROLS_PATH,
            headers=AUTH,
            json={**SUPERTONIC, "overrides": {"steps": 12}},
        )
        assert response.status_code == 200
        assert response.json()["effectiveValues"]["steps"] == 12
        models = client.get("/v1/catalog", headers=AUTH).json()["models"]
        model = next(item for item in models if item["id"] == "supertonic:66m")
        assert model["parameters"]["steps"]["hardRange"] == {
            "min": 1,
            "max": None,
            "minExclusive": False,
        }
        assert model["parameters"]["steps"]["recommendedRange"] == {
            "min": 5,
            "max": 12,
            "minExclusive": False,
        }
        assert model["effectiveValues"]["M1"]["steps"] == 12
        assert model["effectiveValues"]["F1"]["steps"] == 8
        assert "backend" not in model
    finally:
        storage.close()


@pytest.mark.parametrize(
    "body",
    [
        {**SUPERTONIC, "voiceId": "nobody", "overrides": {}},
        {**SUPERTONIC, "overrides": {"total_steps": 12}},
        {**SUPERTONIC, "overrides": {"steps": 0}},
        {**SUPERTONIC, "overrides": {"steps": True}},
        {**SUPERTONIC, "overrides": {"steps": "12"}},
        {**SUPERTONIC, "overrides": {"seed": 10**400}},
    ],
)
def test_a_malformed_update_is_refused_and_keeps_the_stored_value(tmp_path, body):
    entry = supertonic()
    storage = open_test_storage(tmp_path)
    client = TestClient(create_app(token=TOKEN, history=storage))
    try:
        storage.set_control_overrides(entry, "M1", {"steps": 12})
        assert client.patch(CONTROLS_PATH, headers=AUTH, json=body).status_code == 422
        assert (
            storage.effective_controls(entry, "M1").entry.generation_parameters["steps"]
            == 12
        )
    finally:
        storage.close()


def test_an_unknown_model_is_refused(tmp_path):
    storage = open_test_storage(tmp_path)
    client = TestClient(create_app(token=TOKEN, history=storage))
    try:
        response = client.patch(
            CONTROLS_PATH,
            headers=AUTH,
            json={"modelId": "nothing:here", "voiceId": "M1", "overrides": {}},
        )
        assert response.status_code == 404
    finally:
        storage.close()


@pytest.mark.parametrize(
    "model_id,generate,overrides,expected",
    [
        (
            "qwen3-tts:0.6b",
            qwen3.generate,
            {"temperature": 0.6, "top_k": 20, "top_p": 0.8, "decode_mode": "streaming"},
            {"temperature": 0.6, "top_k": 20, "top_p": 0.8, "stream": True},
        ),
    ],
)
def test_declared_overrides_reach_the_model_call(
    model_id, generate, overrides, expected
):
    entry = load_manifest().find(model_id)
    calls = []

    def capture(text, **options):
        calls.append(options)
        return iter(())

    record = GenerationRecord.for_entry(
        entry.compose(overrides).entry, entry.default_voice, "Hello."
    )
    list(generate(SimpleNamespace(generate=capture), record))
    assert {name: calls[0][name] for name in expected} == expected


def test_shared_assembly_controls_do_not_change_segment_identity():
    entry = load_manifest().find("kokoro:82m")
    default = GenerationRecord.for_entry(entry, entry.default_voice, "Hello.")
    composed = entry.compose(
        {
            "pause_sentence_ms": 1000,
            "prepare_first": True,
            "chunk_budget_chars": 200,
            "first_block_chars": 100,
        }
    )
    changed = GenerationRecord.for_entry(
        composed.entry, entry.default_voice, "Hello.", seed=composed.seed
    )
    assert changed.key == default.key


def test_controls_read_preserves_only_overrides_and_validates_voice(tmp_path):
    storage = open_test_storage(tmp_path)
    client = TestClient(create_app(token=TOKEN, history=storage))
    try:
        storage.set_control_overrides(supertonic(), "M1", {"steps": 12})
        assert client.get(CONTROLS_PATH, params=SUPERTONIC).status_code == 401
        response = client.get(CONTROLS_PATH, headers=AUTH, params=SUPERTONIC)
        assert response.status_code == 200
        assert response.json()["overrides"] == {"steps": 12}
        assert response.json()["effectiveValues"]["prepare_first"] is True
        assert (
            client.get(
                CONTROLS_PATH, headers=AUTH, params={**SUPERTONIC, "voiceId": "missing"}
            ).status_code
            == 422
        )
    finally:
        storage.close()


def test_reading_controls_answers_exactly_what_writing_them_answered(tmp_path):
    """A write and a read of the same Voice describe the same Controls.

    The two routes used to build their own payloads, so nothing but care
    kept them shaping the same record. They compose through one helper now,
    and this pins the agreement so a later edit to either cannot drift.
    """
    storage = open_test_storage(tmp_path)
    client = TestClient(create_app(token=TOKEN, history=storage))
    try:
        overrides = {"steps": 12, "prepare_first": True, "pause_sentence_ms": 120}
        written = client.patch(
            CONTROLS_PATH, headers=AUTH, json={**SUPERTONIC, "overrides": overrides}
        )
        assert written.status_code == 200
        read = client.get(CONTROLS_PATH, headers=AUTH, params=SUPERTONIC)
        assert read.status_code == 200
        assert written.json() == read.json()
        assert read.json()["overrides"] == overrides
        assert read.json()["effectiveValues"]["prepare_first"] is True
    finally:
        storage.close()
