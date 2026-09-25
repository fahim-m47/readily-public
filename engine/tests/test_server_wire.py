"""The v1 wire is a client contract: auth, errors, JSON, and SSE framing."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from conftest import AUTH, TOKEN
from fastapi.testclient import TestClient
from storage_fakes import create_plan, open_test_storage, publish_one

from readily_engine.catalog import load_manifest
from readily_engine.catalog.recipes import UnqualifiedRecipe, qualified
from readily_engine.narration.export import ExportDestinationError, ExportInProgress
from readily_engine.server.app import create_app
from readily_engine.storage.history import HistorySchemaError, NarrationStatus
from readily_engine.storage.storage import (
    DeletionResult,
    HistoryDetail,
    HistoryGap,
    HistorySegment,
    NarrationNotResumable,
    RetentionApplied,
    RetentionState,
    VoiceSelection,
)

_STAMP = datetime(2026, 8, 26, tzinfo=UTC)


@pytest.mark.parametrize("speed", [0.5, 1.5, 3.0, 3.5, 4.0])
def test_live_speed_is_an_authenticated_setting_not_a_new_narration(speed):
    narrator = RecordingNarrator()
    client = TestClient(create_app(token=TOKEN, narrator=narrator))
    assert (
        client.patch("/v1/settings/playback", json={"speed": speed}).status_code == 401
    )
    response = client.patch(
        "/v1/settings/playback", json={"speed": speed}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"version": 1, "speed": speed}
    assert narrator.speed == speed
    assert narrator.started == []


@pytest.mark.parametrize("speed", [0, 0.49, 4.01, -1, "NaN", "Infinity"])
def test_speed_outside_the_playback_range_is_rejected(speed):
    client = TestClient(
        create_app(token=TOKEN, narrator=RecordingNarrator(), store=InstalledStore())
    )
    assert (
        client.patch(
            "/v1/settings/playback", json={"speed": speed}, headers=AUTH
        ).status_code
        == 422
    )


class RecordingNarrator:
    def __init__(self) -> None:
        self.started = []
        self.stop_calls = 0
        self.pause_calls = 0
        self.play_calls = 0
        self.seeks = []
        self.time_seeks = []

    def start(self, request):
        entry = load_manifest().resolve(request.model)
        if request.mode == "simple" and qualified(entry, request.voice) is None:
            raise UnqualifiedRecipe("unqualified")
        self.started.append(request)
        return "narration-1"

    def stop(self) -> bool:
        self.stop_calls += 1
        return True

    def pause(self) -> bool:
        self.pause_calls += 1
        return True

    def play(self) -> bool:
        self.play_calls += 1
        return True

    def seek_time(self, position_sec: float) -> bool:
        self.time_seeks.append(position_sec)
        return True

    def seek(self, source_offset: int) -> bool:
        self.seeks.append(source_offset)
        return True

    def set_speed(self, speed: float) -> None:
        self.speed = speed

    async def events(self) -> AsyncIterator[dict[str, object]]:
        yield {
            "version": 1,
            "phase": "idle",
            "narrationId": None,
            "modelId": "kokoro:82m",
            "voiceId": "af_heart",
            "positionSec": 0.0,
            "totalSec": 0.0,
            "speed": 1.0,
            "lateCallbacks": 0,
            "error": None,
        }


class InstalledStore:
    """Every model reads as installed, so speech reaches the narrator."""

    def installed(self, entry) -> bool:
        return True

    def disk_usage(self, entry) -> int:
        return 0

    def delete(self, entry) -> bool:
        return False


class RecordingHistory:
    """The real History views, so a route that reshapes them for the wire
    cannot pass against a fake that has drifted from what storage returns."""

    def __init__(
        self,
        *,
        missing: bool = False,
        status: NarrationStatus = NarrationStatus.INTERRUPTED,
        qualified: bool = True,
    ) -> None:
        self.missing = missing
        self.qualified = qualified
        self.exports: list[tuple[str, str, str]] = []
        self.export_error: Exception | None = None
        self.retention_updates: list[tuple[int, int | None]] = []
        self.selection = VoiceSelection(model_id=None, voice_id=None)
        self.retention_state = RetentionState(
            segment_budget_bytes=5 * 1024**3,
            keep_audio_days=None,
            audio_bytes=321,
        )
        self.item = HistoryDetail(
            id="n-1",
            source_preview="Read locally.",
            model_id="kokoro:82m",
            voice_id="af_heart",
            speed=1.0,
            status=status,
            created_at=_STAMP,
            updated_at=_STAMP,
            last_played_at=_STAMP,
            playhead_sec=0.5,
            total_duration_sec=1.0,
            audio_present=True,
            has_gaps=False,
            source="Read locally.",
            segments=(
                HistorySegment(
                    ordinal=0,
                    source_start=0,
                    source_end=13,
                    boundary="paragraph",
                    duration_sec=1.0,
                    audio_present=True,
                    timeline_start_sec=0.0,
                ),
            ),
            gaps=(),
        )

    def list(self):
        return (self.item,)

    def detail(self, narration_id: str, *, after_ordinal=None):
        if self.missing or narration_id != "n-1":
            return None
        if after_ordinal is None:
            return self.item
        return replace(
            self.item,
            segments=tuple(
                part for part in self.item.segments if part.ordinal > after_ordinal
            ),
        )

    def resume(self, narration_id: str, *, mode="advanced", paused=False) -> str:
        self.resumed_paused = paused
        if mode == "simple" and not self.qualified:
            raise UnqualifiedRecipe("unqualified")
        if self.item.status not in {
            NarrationStatus.INTERRUPTED,
            NarrationStatus.STOPPED,
            NarrationStatus.FINISHED,
        }:
            raise NarrationNotResumable(narration_id)
        return narration_id

    def retention(self) -> RetentionState:
        return self.retention_state

    def update_retention(
        self, *, segment_budget_bytes: int, keep_audio_days: int | None
    ) -> RetentionApplied:
        self.retention_updates.append((segment_budget_bytes, keep_audio_days))
        self.retention_state = RetentionState(
            segment_budget_bytes,
            keep_audio_days,
            self.retention_state.audio_bytes,
        )
        return RetentionApplied(state=self.retention_state, evicted_bytes=64)

    def voice_selection(self) -> VoiceSelection:
        return self.selection

    def select_voice(self, *, model_id: str, voice_id: str) -> VoiceSelection:
        self.selection = VoiceSelection(model_id=model_id, voice_id=voice_id)
        return self.selection

    def delete(self, narration_id: str) -> DeletionResult | None:
        if self.missing or narration_id != "n-1":
            return None
        self.missing = True
        return DeletionResult(narration_id, 123)

    def export(self, narration_id, destination, export_format) -> None:
        if self.export_error is not None:
            raise self.export_error
        self.exports.append((narration_id, str(destination), export_format))

    async def export_events(self) -> AsyncIterator[dict[str, object]]:
        yield {
            "version": 1,
            "phase": "preparing",
            "narrationId": "n-1",
            "format": "m4a",
            "completedBlocks": 0,
            "totalBlocks": 3,
            "error": None,
        }


class UninstalledStore(InstalledStore):
    def installed(self, entry) -> bool:
        return False


def history_client(history: RecordingHistory, store=None) -> TestClient:
    return TestClient(
        create_app(
            token=TOKEN,
            narrator=RecordingNarrator(),
            history=history,
            store=store or InstalledStore(),
        )
    )


def client(narrator: RecordingNarrator | None = None) -> TestClient:
    return TestClient(
        create_app(
            token=TOKEN,
            narrator=narrator or RecordingNarrator(),
            store=InstalledStore(),
        )
    )


def test_auth_failures_use_the_versioned_error_shape():
    response = client().get("/health")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "version": 1,
            "code": "unauthorized",
            "message": "A valid Engine bearer token is required.",
        }
    }


def test_authenticated_unknown_routes_use_the_versioned_error_shape():
    response = client().get("/not-a-route", headers=AUTH)

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "version": 1,
            "code": "not_found",
            "message": "The requested Engine route does not exist.",
        }
    }


def test_speech_accepts_the_frozen_openai_shaped_request():
    narrator = RecordingNarrator()
    response = client(narrator).post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "model": "kokoro:82m",
            "input": "Read this locally.",
            "voice": "af_heart",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "version": 1,
        "narrationId": "narration-1",
        "status": "accepted",
    }
    request = narrator.started[0]
    assert request.model == "kokoro:82m"
    assert request.input == "Read this locally."
    assert request.voice == "af_heart"


def test_speech_accepts_every_catalog_model_with_its_own_voices():
    narrator = RecordingNarrator()
    response = client(narrator).post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"model": "qwen3-tts:0.6b", "input": "Expressively.", "voice": "Chelsie"},
    )

    assert response.status_code == 202
    assert narrator.started[0].model == "qwen3-tts:0.6b"
    assert narrator.started[0].voice == "Chelsie"


def test_speech_defaults_to_the_catalog_default_model_and_its_voice():
    narrator = RecordingNarrator()
    response = client(narrator).post(
        "/v1/audio/speech", headers=AUTH, json={"input": "Defaults."}
    )

    assert response.status_code == 202
    assert narrator.started[0].model == "kokoro:82m"
    assert narrator.started[0].voice == "af_heart"


def test_speech_rejects_a_model_outside_the_catalog():
    response = client().post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"model": "made-up:1b", "input": "hello"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_model"


def test_speech_rejects_a_voice_the_entry_does_not_offer():
    response = client().post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"model": "qwen3-tts:0.6b", "input": "hello", "voice": "af_heart"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_speech_rejects_input_that_is_invisible_rather_than_blank():
    """A zero-width space is not whitespace, so it survives `strip` and
    chunks to nothing — a Narration that can never play, never resume, and
    stays in the History forever."""
    for invisible in ("\u200b", "\u200b \ufeff\u2060", "\u00ad"):
        response = client().post(
            "/v1/audio/speech",
            headers=AUTH,
            json={"model": "kokoro:82m", "input": invisible, "voice": "af_heart"},
        )

        assert response.status_code == 422, invisible
        assert response.json()["error"]["code"] == "invalid_request"


def test_speech_rejects_a_body_of_only_invisible_characters_and_whitespace():
    invisible = "\u200b \n\t\u200b \ufeff  \u2060\r\n" * 50

    response = client().post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"model": "kokoro:82m", "input": invisible, "voice": "af_heart"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == {
        "version": 1,
        "code": "invalid_request",
        "message": "The request body does not match the v1 speech contract.",
    }


def test_speech_accepts_text_that_follows_a_thousand_spaces():
    response = client().post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "model": "kokoro:82m",
            "input": " " * 1000 + "hello",
            "voice": "af_heart",
        },
    )

    assert response.status_code == 202


def test_speech_accepts_input_whose_only_speech_is_punctuation_or_digits():
    for spoken in ("42", "Ok.", "\u00e9t\u00e9"):
        response = client().post(
            "/v1/audio/speech",
            headers=AUTH,
            json={"model": "kokoro:82m", "input": spoken, "voice": "af_heart"},
        )

        assert response.status_code == 202, spoken


def test_speech_rejects_blank_input_with_a_stable_error_code():
    response = client().post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"model": "kokoro:82m", "input": "  ", "voice": "af_heart"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == {
        "version": 1,
        "code": "invalid_request",
        "message": "The request body does not match the v1 speech contract.",
    }


def test_unexpected_route_failures_still_use_the_versioned_error_shape():
    narrator = RecordingNarrator()

    def fail(_request):
        raise ValueError("private implementation detail")

    narrator.start = fail
    app = create_app(token=TOKEN, narrator=narrator, store=InstalledStore())
    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"model": "kokoro:82m", "input": "hello", "voice": "af_heart"},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "version": 1,
            "code": "internal_error",
            "message": "The Engine could not complete the request.",
        }
    }


def test_events_are_authenticated_and_framed_for_streamed_fetch():
    response = client().get("/v1/events", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("retry: 1000\n\n")
    assert "event: narration\n" in response.text
    assert "id: 1\n" in response.text
    assert 'data: {"version":1,"phase":"idle"' in response.text
    assert response.text.endswith("\n\n")


def test_stop_is_idempotent_and_reports_whether_anything_was_active():
    narrator = RecordingNarrator()
    response = client(narrator).post("/v1/audio/stop", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"version": 1, "stopped": True}
    assert narrator.stop_calls == 1


def test_pause_and_play_report_whether_they_applied():
    narrator = RecordingNarrator()
    with client(narrator) as transport:
        paused = transport.post("/v1/audio/pause", headers=AUTH)
        playing = transport.post("/v1/audio/play", headers=AUTH)

    assert paused.status_code == 200
    assert paused.json() == {"version": 1, "paused": True}
    assert playing.status_code == 200
    assert playing.json() == {"version": 1, "playing": True}
    assert narrator.pause_calls == 1
    assert narrator.play_calls == 1


def test_seek_carries_the_target_and_rejects_malformed_bodies():
    narrator = RecordingNarrator()
    with client(narrator) as transport:
        response = transport.post(
            "/v1/audio/seek", json={"sourceOffset": 12}, headers=AUTH
        )
        negative = transport.post(
            "/v1/audio/seek", json={"sourceOffset": -1}, headers=AUTH
        )
        unknown = transport.post(
            "/v1/audio/seek", json={"sourceOffset": 1, "speed": 2}, headers=AUTH
        )

    assert response.status_code == 200
    assert response.json() == {"version": 1, "seeked": True}
    assert narrator.seeks == [12]
    assert negative.status_code == 422
    assert negative.json()["error"]["code"] == "invalid_request"
    assert unknown.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"sourceOffset": 1.5},
        {"sourceOffset": "12"},
        {"sourceOffset": True},
        {"positionSec": -1},
        {"positionSec": "12"},
        {"positionSec": True},
        {"positionSec": 12, "sourceOffset": 1},
        {},
    ],
)
def test_seek_requires_an_integer_source_coordinate(body):
    narrator = RecordingNarrator()
    with client(narrator) as transport:
        response = transport.post("/v1/audio/seek", json=body, headers=AUTH)
    assert response.status_code == 422
    assert narrator.seeks == []


def test_allowed_browser_preflight_needs_no_bearer_token():
    response = client().options(
        "/v1/audio/speech",
        headers={
            "Origin": "tauri://localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_retention_mutations_are_in_the_webview_preflight_allowlist():
    for method, path in (
        ("PATCH", "/v1/settings/retention"),
        ("PATCH", "/v1/settings/voice"),
        ("DELETE", "/v1/history/n-1"),
    ):
        response = client().options(
            path,
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

        assert response.status_code == 200, method
        assert method in response.headers["access-control-allow-methods"]


def test_foreign_browser_preflight_is_not_admitted():
    response = client().options(
        "/v1/audio/speech",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    # The CORS layer answers before any route, so this one refusal is not the
    # error envelope — docs/wire.md documents exactly this body. It still
    # echoes the allowlist's methods/headers, which are inert without the
    # allow-origin the layer withholds.
    assert response.status_code == 400
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.text == "Disallowed CORS origin"
    assert "access-control-allow-origin" not in response.headers


def test_a_wrong_method_on_a_real_route_is_an_envelope_that_keeps_allow():
    response = client().get("/v1/audio/speech", headers=AUTH)

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.json()["error"] == {
        "version": 1,
        "code": "method_not_allowed",
        "message": "The requested Engine route does not accept this method.",
    }


def test_history_list_and_detail_are_safe_versioned_views():
    history = RecordingHistory()
    app = create_app(
        token=TOKEN,
        narrator=RecordingNarrator(),
        history=history,
        store=InstalledStore(),
    )
    client_ = TestClient(app)

    listing = client_.get("/v1/history", headers=AUTH)
    detail = client_.get("/v1/history/n-1", headers=AUTH)

    assert listing.status_code == 200
    assert listing.json()["history"][0]["sourcePreview"] == "Read locally."
    assert detail.json()["source"] == "Read locally."
    assert detail.json()["segments"][0] == {
        "ordinal": 0,
        "sourceStart": 0,
        "sourceEnd": 13,
        "boundary": "paragraph",
        "durationSec": 1.0,
        "audioPresent": True,
        "timelineStartSec": 0.0,
        "timings": [],
    }
    serialized = listing.text + detail.text
    assert "segment_hash" not in serialized
    assert "/Users/" not in serialized


def three_blocks(history: RecordingHistory) -> None:
    """Three Blocks, the middle one a gap: a gap before the cursor must still
    come back, or the shell could never learn that it was filled."""
    first = history.item.segments[0]
    history.item = replace(
        history.item,
        segments=tuple(
            replace(first, ordinal=ordinal, source_start=ordinal * 4)
            for ordinal in range(3)
        ),
        gaps=(
            HistoryGap(
                ordinal=1,
                source_start=4,
                source_end=8,
                error_code="synthesis_failed",
                created_at=history.item.created_at,
            ),
        ),
    )


def test_history_detail_after_an_ordinal_carries_only_the_blocks_past_it():
    history = RecordingHistory()
    three_blocks(history)
    client_ = history_client(history)

    whole = client_.get("/v1/history/n-1", headers=AUTH)
    from_start = client_.get("/v1/history/n-1?afterOrdinal=0", headers=AUTH)
    tail = client_.get("/v1/history/n-1?afterOrdinal=1", headers=AUTH)
    past_the_end = client_.get("/v1/history/n-1?afterOrdinal=7", headers=AUTH)

    assert [part["ordinal"] for part in whole.json()["segments"]] == [0, 1, 2]
    assert whole.json()["source"] == "Read locally."
    assert "source" not in from_start.json()
    assert [part["ordinal"] for part in from_start.json()["segments"]] == [1, 2]
    assert tail.status_code == 200
    assert "source" not in tail.json()
    assert [part["ordinal"] for part in tail.json()["segments"]] == [2]
    assert [gap["ordinal"] for gap in tail.json()["gaps"]] == [1]
    assert tail.json()["totalDurationSec"] == whole.json()["totalDurationSec"]
    assert past_the_end.status_code == 200
    assert past_the_end.json()["segments"] == []


@pytest.mark.parametrize("value", ["-1", "1.5", "one", ""])
def test_history_detail_refuses_a_cursor_that_is_not_a_whole_number(value):
    response = history_client(RecordingHistory()).get(
        f"/v1/history/n-1?afterOrdinal={value}", headers=AUTH
    )
    assert response.status_code == 422, value
    assert response.json()["error"]["code"] == "invalid_request"


def test_resume_is_explicit_and_returns_the_existing_accepted_shape():
    history = RecordingHistory()
    response = history_client(history).post("/v1/history/n-1/resume", headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {
        "version": 1,
        "narrationId": "n-1",
        "status": "accepted",
    }


def test_resume_can_open_a_narration_paused():
    history = RecordingHistory()
    response = history_client(history).post(
        "/v1/history/n-1/resume", params={"paused": "true"}, headers=AUTH
    )
    assert response.status_code == 202
    assert history.resumed_paused is True


def test_resume_maps_unknown_nonresumable_and_missing_model():
    assert (
        history_client(RecordingHistory(missing=True))
        .post("/v1/history/missing/resume", headers=AUTH)
        .status_code
        == 404
    )
    replay = history_client(RecordingHistory(status=NarrationStatus.FINISHED)).post(
        "/v1/history/n-1/resume", headers=AUTH
    )
    assert replay.status_code == 202

    missing_model = history_client(RecordingHistory(), store=UninstalledStore()).post(
        "/v1/history/n-1/resume", headers=AUTH
    )
    assert missing_model.status_code == 409
    assert missing_model.json()["error"]["code"] == "model_not_installed"

    both = history_client(
        RecordingHistory(status=NarrationStatus.FAILED), store=UninstalledStore()
    ).post("/v1/history/n-1/resume", headers=AUTH)
    assert both.status_code == 409
    assert both.json()["error"]["code"] == "model_not_installed"


def test_resume_in_simple_refuses_history_off_the_qualified_recipe():
    refused = history_client(RecordingHistory(qualified=False)).post(
        "/v1/history/n-1/resume", params={"mode": "simple"}, headers=AUTH
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "recipe_not_qualified"
    advanced = history_client(RecordingHistory(qualified=False)).post(
        "/v1/history/n-1/resume", headers=AUTH
    )
    assert advanced.status_code == 202


def test_export_is_accepted_immediately_with_the_format_it_will_write():
    # Accepted, not finished: a Narration whose audio has been evicted is
    # re-synthesized behind whatever is playing, and the client watches
    # /v1/export/events for that. The press costs the same either way.
    history = RecordingHistory()
    response = history_client(history).post(
        "/v1/history/n-1/export",
        headers=AUTH,
        json={"destination": "/Users/reader/Desktop/read.m4a", "format": "wav"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "version": 1,
        "narrationId": "n-1",
        "format": "wav",
        "status": "accepted",
    }
    assert history.exports == [("n-1", "/Users/reader/Desktop/read.m4a", "wav")]


def test_export_defaults_to_m4a_and_refuses_every_other_format():
    history = RecordingHistory()
    transport = history_client(history)

    default = transport.post(
        "/v1/history/n-1/export",
        headers=AUTH,
        json={"destination": "/Users/reader/Desktop/read.m4a"},
    )
    mp3 = transport.post(
        "/v1/history/n-1/export",
        headers=AUTH,
        json={"destination": "/Users/reader/Desktop/read.mp3", "format": "mp3"},
    )

    assert default.status_code == 202
    assert history.exports == [("n-1", "/Users/reader/Desktop/read.m4a", "m4a")]
    assert mp3.status_code == 422


def test_export_maps_unknown_busy_and_refused_destinations():
    missing = history_client(RecordingHistory(missing=True)).post(
        "/v1/history/missing/export",
        headers=AUTH,
        json={"destination": "/Users/reader/Desktop/read.m4a"},
    )
    assert missing.status_code == 404

    busy_history = RecordingHistory()
    busy_history.export_error = ExportInProgress("n-1")
    busy = history_client(busy_history).post(
        "/v1/history/n-1/export",
        headers=AUTH,
        json={"destination": "/Users/reader/Desktop/read.m4a"},
    )
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "export_in_progress"

    shutting_down = RecordingHistory()
    shutting_down.export_error = RuntimeError("the Engine is shutting down")
    unavailable = history_client(shutting_down).post(
        "/v1/history/n-1/export",
        headers=AUTH,
        json={"destination": "/Users/reader/Desktop/read.m4a"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "engine_unavailable"

    refused_history = RecordingHistory()
    refused_history.export_error = ExportDestinationError("nope")
    refused = history_client(refused_history).post(
        "/v1/history/n-1/export",
        headers=AUTH,
        json={"destination": "read.m4a"},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_destination"


def test_a_cached_export_does_not_need_the_voice_model_installed():
    # ADR 0004 §3: the lossless Segment cache is the source of truth, so an
    # Export that needs no synthesis must not be refused for a model the
    # user has since deleted.
    history = RecordingHistory()
    cached = history_client(history, store=UninstalledStore()).post(
        "/v1/history/n-1/export",
        headers=AUTH,
        json={"destination": "/Users/reader/Desktop/read.m4a"},
    )
    assert cached.status_code == 202

    evicted_history = RecordingHistory()
    evicted_history.item = replace(
        evicted_history.item,
        audio_present=False,
        segments=(replace(evicted_history.item.segments[0], audio_present=False),),
    )
    evicted = history_client(evicted_history, store=UninstalledStore()).post(
        "/v1/history/n-1/export",
        headers=AUTH,
        json={"destination": "/Users/reader/Desktop/read.m4a"},
    )
    assert evicted.status_code == 409
    assert evicted.json()["error"]["code"] == "model_not_installed"


@pytest.mark.parametrize("damage", ["replaced", "digestless"])
def test_export_of_unverified_audio_requires_the_deleted_model(tmp_path, damage):
    storage = open_test_storage(tmp_path)

    class StoredHistory(RecordingHistory):
        def detail(self, narration_id, *, after_ordinal=None):
            del after_ordinal
            return storage.history_detail(narration_id)

    history = StoredHistory()
    try:
        plan = create_plan(storage, "Read locally.")
        publish_one(storage, plan)
        flac = next((tmp_path / "segments").rglob("*.flac"))
        if damage == "replaced":
            flac.write_bytes(b"replaced audio")
        else:
            flac.with_suffix(".frames").write_text("240\n")
        response = history_client(history, store=UninstalledStore()).post(
            f"/v1/history/{plan.id}/export",
            headers=AUTH,
            json={"destination": str(tmp_path / "read.wav"), "format": "wav"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "model_not_installed"
        assert history.exports == []
    finally:
        storage.close()


def test_a_recorded_gap_does_not_make_an_export_need_the_voice_model():
    # `audioPresent` is false for the life of a Narration with a gap: the
    # Block has a key and no audio, and always will. Export replays it as a
    # gap and never reaches a synthesizer, so gating on `audioPresent` alone
    # would make such a Narration unexportable forever once the model is gone.
    history = RecordingHistory()
    history.item = replace(
        history.item,
        audio_present=False,
        has_gaps=True,
        segments=(
            replace(history.item.segments[0], audio_present=False),
            HistorySegment(
                ordinal=1,
                source_start=13,
                source_end=20,
                boundary="paragraph",
                duration_sec=1.0,
                audio_present=True,
                timeline_start_sec=1.0,
            ),
        ),
        gaps=(
            HistoryGap(
                ordinal=0,
                source_start=0,
                source_end=13,
                error_code="generation_failed",
                created_at=_STAMP,
            ),
        ),
    )

    response = history_client(history, store=UninstalledStore()).post(
        "/v1/history/n-1/export",
        headers=AUTH,
        json={"destination": "/Users/reader/Desktop/read.m4a"},
    )

    assert response.status_code == 202
    assert history.exports == [("n-1", "/Users/reader/Desktop/read.m4a", "m4a")]


def test_export_events_are_framed_on_their_own_stream():
    response = history_client(RecordingHistory()).get("/v1/export/events", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("retry: 1000\n\n")
    assert "event: export\n" in response.text
    assert '"totalBlocks":3' in response.text


def test_retention_settings_report_split_usage_and_update_both_knobs():
    # A flat per-entry cost, summed over however many entries the committed
    # Catalog carries: the split this reports is models against audio, not
    # one model against another, and adding an entry should not edit a total.
    models_bytes = 100 * len(load_manifest().models)

    class UsageStore(InstalledStore):
        def disk_usage(self, entry) -> int:
            return 100

    history = RecordingHistory()
    with history_client(history, store=UsageStore()) as transport:
        before = transport.get("/v1/settings/retention", headers=AUTH)
        after = transport.patch(
            "/v1/settings/retention",
            headers=AUTH,
            json={"segmentBudgetBytes": 1_000_000, "keepAudioDays": 30},
        )

    assert before.status_code == 200
    assert before.json() == {
        "version": 1,
        "segmentBudgetBytes": 5 * 1024**3,
        "keepAudioDays": None,
        "diskUsage": {"modelsBytes": models_bytes, "audioBytes": 321},
    }
    assert after.status_code == 200
    assert after.json()["segmentBudgetBytes"] == 1_000_000
    assert after.json()["keepAudioDays"] == 30
    # What applying the policy took, from the sweep that took it. `GET`
    # carries no such field, because nothing was applied to report on.
    assert after.json()["evictedBytes"] == 64
    assert "evictedBytes" not in before.json()
    assert history.retention_updates == [(1_000_000, 30)]


def test_retention_update_rejects_partial_invalid_and_unknown_fields():
    transport = history_client(RecordingHistory())

    for body in (
        {"segmentBudgetBytes": 1000},
        {"segmentBudgetBytes": 0, "keepAudioDays": None},
        {"segmentBudgetBytes": 1000, "keepAudioDays": -1},
        {"segmentBudgetBytes": 1000, "keepAudioDays": None, "speed": 2},
    ):
        response = transport.patch("/v1/settings/retention", headers=AUTH, json=body)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "invalid_request"


def test_retention_update_rejects_values_storage_could_not_survive():
    """Both knobs are bounded above, not just below.

    A budget past SQLite's INTEGER range or an age that drives the eviction
    cutoff below `datetime.min` used to reach storage and raise. The age
    case was the worse one: the row was committed before it was applied, so
    every later sweep — including the one at startup — raised on it.
    """
    history = RecordingHistory()
    transport = history_client(history)

    for body in (
        {"segmentBudgetBytes": 2**63, "keepAudioDays": None},
        {"segmentBudgetBytes": 1000, "keepAudioDays": 36_501},
        {"segmentBudgetBytes": 1000, "keepAudioDays": 10**9},
    ):
        response = transport.patch("/v1/settings/retention", headers=AUTH, json=body)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "invalid_request"

    assert history.retention_updates == []


def test_a_chosen_voice_is_stored_and_read_back_the_way_a_picker_needs_it():
    """The pill's whole promise: what was chosen is what comes back, after
    a restart as well as before one."""
    history = RecordingHistory()
    with history_client(history) as transport:
        fresh = transport.get("/v1/settings/voice", headers=AUTH)
        chosen = transport.patch(
            "/v1/settings/voice",
            headers=AUTH,
            json={"modelId": "qwen3-tts", "voiceId": "Chelsie"},
        )
        again = transport.get("/v1/settings/voice", headers=AUTH)

    # Nothing chosen yet is the Catalog's own default, not an empty answer
    # a picker would have to invent a Voice for.
    assert fresh.json() == {
        "version": 1,
        "modelId": "kokoro:82m",
        "voiceId": "af_heart",
    }
    assert chosen.status_code == 200
    # Stored resolved: a bare name is a reference, and `name:tag` is what a
    # later release can still resolve.
    assert history.selection == VoiceSelection("qwen3-tts:0.6b", "Chelsie")
    assert again.json() == {
        "version": 1,
        "modelId": "qwen3-tts:0.6b",
        "voiceId": "Chelsie",
    }


def test_choosing_a_voice_the_catalog_does_not_offer_is_refused():
    history = RecordingHistory()
    transport = history_client(history)

    unknown_model = transport.patch(
        "/v1/settings/voice",
        headers=AUTH,
        json={"modelId": "not-a-model", "voiceId": "af_heart"},
    )
    # A real entry, but a Voice it does not offer — the same refusal
    # `/v1/audio/speech` gives for the same mistake.
    wrong_voice = transport.patch(
        "/v1/settings/voice",
        headers=AUTH,
        json={"modelId": "kokoro:82m", "voiceId": "Chelsie"},
    )
    extra_field = transport.patch(
        "/v1/settings/voice",
        headers=AUTH,
        json={"modelId": "kokoro:82m", "voiceId": "af_heart", "speed": 2},
    )

    assert unknown_model.status_code == 404
    assert unknown_model.json()["error"]["code"] == "unknown_model"
    assert wrong_voice.status_code == 422
    assert wrong_voice.json()["error"]["code"] == "invalid_request"
    assert extra_field.status_code == 422
    assert history.selection == VoiceSelection(None, None)


def test_a_stored_choice_the_catalog_has_since_retired_falls_back():
    """Entries come and go between releases; a picker handed an id that
    resolves to nothing has no Voice Model to draw."""
    history = RecordingHistory()
    history.selection = VoiceSelection("retired:1b", "somebody")
    transport = history_client(history)

    response = transport.get("/v1/settings/voice", headers=AUTH)

    assert response.json() == {
        "version": 1,
        "modelId": "kokoro:82m",
        "voiceId": "af_heart",
    }


def test_a_stored_voice_its_model_no_longer_offers_falls_back_to_that_model():
    history = RecordingHistory()
    history.selection = VoiceSelection("kokoro:82m", "af_retired")
    transport = history_client(history)

    response = transport.get("/v1/settings/voice", headers=AUTH)

    # The Voice Model stands; only the Voice is replaced, by its own
    # default rather than by the Catalog's.
    assert response.json()["modelId"] == "kokoro:82m"
    assert response.json()["voiceId"] == "af_heart"


def test_delete_and_retention_report_unavailable_storage_as_503():
    """`404` on a delete reads as "already gone" and a client acts on it."""
    transport = TestClient(
        create_app(token=TOKEN, narrator=RecordingNarrator(), store=InstalledStore())
    )

    bodies = {
        "/v1/settings/retention": {"segmentBudgetBytes": 1000, "keepAudioDays": None},
        "/v1/settings/voice": {"modelId": "kokoro", "voiceId": "af_heart"},
    }
    for method, path in (
        ("GET", "/v1/settings/retention"),
        ("PATCH", "/v1/settings/retention"),
        ("GET", "/v1/settings/voice"),
        ("PATCH", "/v1/settings/voice"),
        ("DELETE", "/v1/history/n-1"),
    ):
        response = transport.request(
            method,
            path,
            headers=AUTH,
            json=bodies[path] if method == "PATCH" else None,
        )
        assert response.status_code == 503, (method, path)
        assert response.json()["error"]["code"] == "engine_unavailable"


def test_delete_history_reports_freed_audio_and_then_not_found():
    history = RecordingHistory()
    transport = history_client(history)

    deleted = transport.delete("/v1/history/n-1", headers=AUTH)
    missing = transport.delete("/v1/history/n-1", headers=AUTH)

    assert deleted.status_code == 200
    assert deleted.json() == {
        "version": 1,
        "narrationId": "n-1",
        "deleted": True,
        "audioBytesFreed": 123,
    }
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_a_corrupt_history_schema_is_not_reported_as_retryable():
    """`HistorySchemaError` is a `RuntimeError`, so the unavailable-Engine
    catch would answer `503` for a database that is never going to repair
    itself — and a client that backs off and retries would do so forever."""

    class CorruptHistory(RecordingHistory):
        def retention(self) -> RetentionState:
            raise HistorySchemaError("The settings singleton is missing")

        def update_retention(
            self, *, segment_budget_bytes: int, keep_audio_days: int | None
        ) -> RetentionApplied:
            raise HistorySchemaError("The settings singleton is missing")

        def delete(self, narration_id: str):
            raise HistorySchemaError("The settings singleton is missing")

        def voice_selection(self) -> VoiceSelection:
            raise HistorySchemaError("The settings singleton is missing")

        def select_voice(self, *, model_id: str, voice_id: str) -> VoiceSelection:
            raise HistorySchemaError("The settings singleton is missing")

    transport = TestClient(
        create_app(
            token=TOKEN,
            narrator=RecordingNarrator(),
            history=CorruptHistory(),
            store=InstalledStore(),
        ),
        raise_server_exceptions=False,
    )

    for method, path, body in (
        ("GET", "/v1/settings/retention", None),
        (
            "PATCH",
            "/v1/settings/retention",
            {"segmentBudgetBytes": 1, "keepAudioDays": None},
        ),
        ("GET", "/v1/settings/voice", None),
        (
            "PATCH",
            "/v1/settings/voice",
            {"modelId": "kokoro:82m", "voiceId": "af_heart"},
        ),
        ("DELETE", "/v1/history/n-1", None),
    ):
        response = transport.request(method, path, headers=AUTH, json=body)
        assert response.status_code == 500, (method, path)
        assert response.json()["error"]["code"] == "internal_error"


def test_simple_admission_rejects_unqualified_voices_before_generation():
    narrator = RecordingNarrator()
    response = client(narrator).post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "model": "qwen3-tts:0.6b",
            "input": "Stay local.",
            "voice": "Chelsie",
            "mode": "simple",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "recipe_not_qualified"
    assert narrator.started == []


def test_simple_admission_passes_mode_to_the_engine():
    narrator = RecordingNarrator()
    response = client(narrator).post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "Stay local.", "mode": "simple"},
    )
    assert response.status_code == 202
    assert narrator.started[0].mode == "simple"


def test_time_seek_carries_exact_seconds():
    narrator = RecordingNarrator()
    with client(narrator) as transport:
        response = transport.post(
            "/v1/audio/seek", json={"positionSec": 24.5}, headers=AUTH
        )
    assert response.status_code == 200
    assert response.json() == {"version": 1, "seeked": True}
    assert narrator.time_seeks == [24.5]
    assert narrator.seeks == []
