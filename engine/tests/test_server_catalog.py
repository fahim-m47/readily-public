"""`GET /v1/catalog` enumerates the Catalog with nothing downloaded.

The Manifest is baked into the release, so this route answers on a machine
that has never been online and holds no weights (ADR 0003 §1). It is the
picker's data source, so it carries what a user chooses between — Tier,
Voices, licence, download size — and not the curation facts behind it.
"""

from conftest import AUTH, TOKEN
from fastapi.testclient import TestClient

from readily_engine.catalog import load_manifest
from readily_engine.catalog.licences import licence_text
from readily_engine.server.app import create_app


def client() -> TestClient:
    return TestClient(create_app(token=TOKEN))


def catalog() -> dict:
    response = client().get("/v1/catalog", headers=AUTH)
    assert response.status_code == 200
    return response.json()


def test_the_catalog_needs_the_launch_token_like_every_other_route():
    assert client().get("/v1/catalog").status_code == 401


def test_the_catalog_carries_the_wire_version():
    assert catalog()["version"] == 1


def test_the_catalog_enumerates_every_manifest_entry():
    assert [model["id"] for model in catalog()["models"]] == [
        entry.id for entry in load_manifest().models
    ]


def test_the_catalog_names_the_model_a_fresh_install_starts_with():
    payload = catalog()

    assert payload["defaultModelId"] == "kokoro:82m"
    assert payload["defaultModelId"] in {model["id"] for model in payload["models"]}


def test_an_entry_carries_what_the_picker_renders():
    kokoro = next(m for m in catalog()["models"] if m["id"] == "kokoro:82m")

    assert kokoro["name"] == "Kokoro"
    assert kokoro["tier"] == "instant"
    assert kokoro["license"] == "Apache-2.0"
    assert kokoro["defaultVoiceId"] == "af_heart"
    assert {
        "id": "af_heart",
        "name": "Heart (Female)",
        "language": "en-US",
        "preview": "kokoro/82m/af_heart.m4a",
        "simple": True,
    } in kokoro["voices"]


def test_an_entry_reports_what_downloading_it_costs():
    kokoro = next(m for m in catalog()["models"] if m["id"] == "kokoro:82m")
    entry = load_manifest().find("kokoro:82m")

    assert kokoro["downloadBytes"] == sum(file.size_bytes for file in entry.files)


def test_the_catalog_withholds_curation_facts_the_client_has_no_use_for():
    # Hashes, revisions and the Backend are how the Engine fetches and runs a
    # model, not how a user picks one — and Backend is explicitly not user
    # vocabulary (CONTEXT.md). Keeping them off the wire keeps the picker
    # honest about what it is choosing between.
    withheld = {
        "files",
        "source",
        "backend",
        "provenance",
        "copyright_notice",
        "copyright_source",
    }
    for model in catalog()["models"]:
        assert not withheld & set(model)
        assert "copyrightNotice" not in model


def test_an_entry_says_what_running_it_costs_in_memory():
    # A RAM class per entry, so a client can warn
    # before a download rather than after an out-of-memory load.
    by_id = {model["id"]: model for model in catalog()["models"]}

    assert by_id["kokoro:82m"]["ramClassGb"] == 0.5
    assert by_id["qwen3-tts:0.6b"]["ramClassGb"] == 3


def test_a_voice_carries_its_bundled_preview_clip_or_says_it_has_none():
    # The clip is an app resource, not an Engine one — the wire carries
    # where it is, never the audio (ADR 0003 consequences). Every Voice
    # answers the field, so a Voice curation has not clipped yet says
    # `None` rather than leaving a client to guess at a path that 404s.
    pinned = {
        (entry.id, voice.id): voice.preview
        for entry in load_manifest().models
        for voice in entry.voices
    }

    # The Voice reference is the Engine's conditioning input, not a thing
    # the client plays or shows, so it stays off the wire with the Backend.
    for model in catalog()["models"]:
        for voice in model["voices"]:
            assert set(voice) == {"id", "name", "language", "preview", "simple"}
            assert voice["preview"] == pinned[model["id"], voice["id"]]


def test_an_entry_carries_its_licence_in_full_so_the_sheet_can_show_it():
    kokoro = next(m for m in catalog()["models"] if m["id"] == "kokoro:82m")

    assert kokoro["licenseTerms"] == {
        "id": "Apache-2.0",
        "name": "Apache 2.0",
        "bindsReader": False,
        "credit": None,
        "attribution": None,
        "text": licence_text("Apache-2.0"),
    }


def test_a_store_written_notice_licence_shows_its_holder_line_in_the_sheet():
    manifest = load_manifest()
    entry = type(manifest.models[0]).model_validate(
        {
            **manifest.models[0].model_dump(mode="json"),
            "license": "MIT",
            "copyright_notice": "Copyright (c) 2025 Resemble AI",
            "copyright_source": "https://example.com/resemble/LICENSE",
        }
    )
    response = TestClient(
        create_app(token=TOKEN, catalog=manifest.model_copy(update={"models": [entry]}))
    ).get("/v1/catalog", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["models"][0]["licenseTerms"]["text"].startswith(
        "MIT License\n\nCopyright (c) 2025 Resemble AI\n\nPermission is hereby granted"
    )


def test_only_a_licence_that_asks_for_attribution_carries_the_entry_s_own():
    attribution = {
        "creator": "Acme Audio",
        "copyright_notice": ("Copyright 2026 Acme Audio\nCopyright 2025 Contributors"),
        "modified": True,
    }
    manifest = load_manifest()
    entry = type(manifest.models[0]).model_validate(
        {
            **manifest.models[0].model_dump(mode="json"),
            "license": "CC-BY-4.0",
            "attribution": attribution,
        }
    )
    catalog = manifest.model_copy(update={"models": [entry]})
    response = TestClient(create_app(token=TOKEN, catalog=catalog)).get(
        "/v1/catalog", headers=AUTH
    )
    assert response.status_code == 200

    assert response.json()["models"][0]["licenseTerms"]["attribution"] == {
        "creator": "Acme Audio",
        "copyrightNotice": ("Copyright 2026 Acme Audio\nCopyright 2025 Contributors"),
        "source": entry.source.page_url,
        "warrantyNotice": (
            "Section 5 \N{EN DASH} Disclaimer of Warranties and Limitation of "
            "Liability."
        ),
        "modified": True,
    }


def test_support_memory_download_and_licence_are_charged_only_where_needed():
    manifest = load_manifest()
    models = {model["id"]: model for model in catalog()["models"]}
    for entry in manifest.models:
        support = manifest.required_support(
            entry.timing_choice(voice.id) for voice in entry.voices
        )
        row = models[entry.id]
        assert row["ramClassGb"] == entry.ram_class_gb + sum(
            s.ram_class_gb for s in support
        )
        assert row["downloadBytes"] == entry.download_bytes + sum(
            s.download_bytes for s in support
        )
        assert [s["name"] for s in row["supportModels"]] == [
            s.display_name for s in support
        ]
