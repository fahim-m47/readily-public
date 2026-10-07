"""The Engine's versioned HTTP/SSE app, admitted by the B3 middleware."""

from collections.abc import AsyncIterator, Callable
from typing import Annotated, Literal, Protocol

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from readily_engine.audio import MAX_PLAYBACK_SPEED, MIN_PLAYBACK_SPEED
from readily_engine.audio.encoding import ExportFormat, export_encoders
from readily_engine.catalog import (
    LICENCE_OBLIGATIONS,
    CatalogEntry,
    Manifest,
    PinnedArtifact,
    SupportModel,
    licence_text,
    licence_text_for,
    load_manifest,
)
from readily_engine.catalog.controls import Overrides
from readily_engine.catalog.manifest import Effective
from readily_engine.catalog.recipes import Mode, qualified
from readily_engine.chunking import has_text
from readily_engine.download.environment import default_data_dir
from readily_engine.download.fetch import fetch_entry
from readily_engine.download.link import LinkError, LinkFailure, Page, fetch_page
from readily_engine.loading.architecture import Backend
from readily_engine.loading.registry import available_backends
from readily_engine.narration.export import ExportInProgress
from readily_engine.server.advanced import advanced_router
from readily_engine.server.auth import (
    BearerTokenMiddleware,
    OriginAllowlistMiddleware,
    allowed_origins,
)
from readily_engine.server.entries import (
    Store,
    VoiceRequest,
    default_here,
    regeneration_refusal,
    resolve_voice,
    runs_here,
)
from readily_engine.server.wire import (
    ERROR_MESSAGES,
    WIRE_VERSION,
    ErrorCode,
    error_response,
    sse_event,
)
from readily_engine.storage.history import (
    MAX_KEEP_AUDIO_DAYS,
    MAX_SEGMENT_BUDGET_BYTES,
    HistorySchemaError,
)
from readily_engine.storage.storage import (
    DeletionResult,
    HistoryDetail,
    HistoryGap,
    HistorySegment,
    HistorySummary,
    NarrationNotResumable,
    RetentionApplied,
    RetentionState,
    VoiceSelection,
)
from readily_engine.store import DeleteOutcome, DownloadManager, ModelStore


class SpeechRequest(BaseModel):
    """The supported subset of OpenAI's speech request, v1.

    `model` and `voice` are validated against the baked Catalog in the
    route, not here — a model reference resolves like every other model
    route's, and each entry names the voices it offers.
    """

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    input: str = Field(max_length=1_000_000)
    voice: str | None = None
    mode: Mode = "advanced"

    @field_validator("input")
    @classmethod
    def input_must_contain_text(cls, value: str) -> str:
        if not has_text(value):
            raise ValueError("input must contain text")
        return value


class PlaybackSettingsRequest(BaseModel):
    """A live speed, shared by every Voice Model and persisted across starts."""

    model_config = ConfigDict(extra="forbid")

    speed: float = Field(
        ge=MIN_PLAYBACK_SPEED,
        le=MAX_PLAYBACK_SPEED,
        allow_inf_nan=False,
    )


class SeekRequest(BaseModel):
    """A Unicode code-point offset into the active Narration's Source."""

    model_config = ConfigDict(extra="forbid")

    sourceOffset: int = Field(ge=0, strict=True)


class TimeSeekRequest(BaseModel):
    """An exact position in the Narration, measured in source seconds."""

    model_config = ConfigDict(extra="forbid")

    positionSec: float = Field(ge=0, allow_inf_nan=False, strict=True)


class ExportRequest(BaseModel):
    """Which format to write a Narration in, or none for this machine's
    default: M4A where the Engine can write it, WAV everywhere else (ADR 0015).

    Never where: the Engine names the file and the folder itself (ADR 0004
    §4), so the webview has no path to steer (threat model B4). There is no
    MP3 to ask for.
    """

    model_config = ConfigDict(extra="forbid")

    format: ExportFormat | None = None


class SourceFetchRequest(BaseModel):
    """A link the reader asked to read (`POST /v1/sources/fetch`)."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)


# How each reason a link was not read goes on the wire: the reader's own
# request refused, or the far end at fault.
LINK_ERRORS: dict[LinkFailure, tuple[int, ErrorCode]] = {
    "refused": (422, ErrorCode.LINK_REFUSED),
    "unreachable": (502, ErrorCode.LINK_UNREACHABLE),
    "too_large": (502, ErrorCode.LINK_TOO_LARGE),
    "not_a_page": (502, ErrorCode.LINK_NOT_A_PAGE),
}


class RetentionRequest(BaseModel):
    """The complete pair of user-controlled Segment retention knobs.

    Both carry an upper bound as well as a lower one. Unbounded, a budget
    past SQLite's INTEGER range or an age that drives the eviction cutoff
    below `datetime.min` is accepted here and only fails once storage tries
    to use it — a `500` where the contract promises `422`, and in the age
    case a policy row that is committed before it is applied.
    """

    model_config = ConfigDict(extra="forbid")

    segmentBudgetBytes: int = Field(gt=0, le=MAX_SEGMENT_BUDGET_BYTES)
    keepAudioDays: int | None = Field(ge=0, le=MAX_KEEP_AUDIO_DAYS)


class Narrator(Protocol):
    """What HTTP needs from generation; implementation lives outside B3."""

    def start(self, request: SpeechRequest) -> str: ...

    def stop(self) -> bool: ...

    def set_speed(self, speed: float) -> None: ...

    def pause(self) -> bool: ...

    def play(self) -> bool: ...

    def seek(self, source_offset: int) -> bool: ...

    def seek_time(self, position_sec: float) -> bool: ...

    def select_take(
        self, narration_id: str, ordinal: int, action: Literal["reroll", "A", "B"]
    ) -> bool: ...

    def events(self) -> AsyncIterator[dict[str, object]]: ...


class History(Protocol):
    """What HTTP needs from durable Narration History; SQLite and FLAC
    stay behind `NarrationStorage` (B3), which is also where these views
    are defined — routes shape them for the wire, they do not re-derive
    them."""

    def list(self) -> tuple[HistorySummary, ...]: ...

    def detail(
        self, narration_id: str, *, after_ordinal: int | None = None
    ) -> HistoryDetail | None: ...

    def resume(self, narration_id: str, *, paused: bool = False) -> str: ...

    def delete(self, narration_id: str) -> DeletionResult | None: ...

    def retention(self) -> RetentionState: ...

    def update_retention(
        self, *, segment_budget_bytes: int, keep_audio_days: int | None
    ) -> RetentionApplied: ...

    def voice_selection(self) -> VoiceSelection: ...

    def select_voice(self, *, model_id: str, voice_id: str) -> VoiceSelection: ...

    def effective_controls(self, entry: CatalogEntry, voice_id: str) -> Effective: ...

    def control_overrides(self, entry: CatalogEntry, voice_id: str) -> Overrides: ...

    def set_control_overrides(
        self, entry: CatalogEntry, voice_id: str, overrides: Overrides
    ) -> Effective: ...

    def export(self, narration_id: str, export_format: ExportFormat) -> None: ...

    def export_events(self) -> AsyncIterator[dict[str, object]]: ...


class Downloads(Protocol):
    """What HTTP needs from the download manager — including deleting, which
    is queued with downloads there rather than ordered here (ADR 0003 §3)."""

    def start(
        self, entry: CatalogEntry, *, support: tuple[SupportModel, ...] = ()
    ) -> None: ...

    def delete(self, entry: CatalogEntry) -> DeleteOutcome: ...

    def withdraw(self, entry: CatalogEntry) -> bool: ...

    def events(self) -> AsyncIterator[dict[str, object]]: ...


class UnavailableNarrator:
    """Keeps health/auth unit tests independent of audio dependencies."""

    def start(self, request: SpeechRequest) -> str:
        del request
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    def stop(self) -> bool:
        return False

    def set_speed(self, speed: float) -> None:
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    def pause(self) -> bool:
        return False

    def play(self) -> bool:
        return False

    def seek(self, source_offset: int) -> bool:
        del source_offset
        return False

    def seek_time(self, position_sec: float) -> bool:
        del position_sec
        return False

    async def events(self) -> AsyncIterator[dict[str, object]]:
        if False:
            yield {}


class UnavailableHistory:
    """Keeps health/auth unit tests independent of storage."""

    def list(self) -> tuple[HistorySummary, ...]:
        return ()

    def detail(
        self, narration_id: str, *, after_ordinal: int | None = None
    ) -> HistoryDetail | None:
        del narration_id, after_ordinal
        return None

    def resume(self, narration_id: str, *, paused: bool = False) -> str:
        del narration_id, paused
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    def delete(self, narration_id: str) -> DeletionResult | None:
        # Not `None`: the route reads that as "no such Narration", and a
        # client told its delete succeeded drops the row it still has.
        del narration_id
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    def retention(self) -> RetentionState:
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    def update_retention(
        self, *, segment_budget_bytes: int, keep_audio_days: int | None
    ) -> RetentionApplied:
        del segment_budget_bytes, keep_audio_days
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    def voice_selection(self) -> VoiceSelection:
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    def effective_controls(self, entry: CatalogEntry, voice_id: str) -> Effective:
        del voice_id
        return entry.compose({})

    def control_overrides(self, entry: CatalogEntry, voice_id: str) -> Overrides:
        return {}

    def set_control_overrides(
        self, entry: CatalogEntry, voice_id: str, overrides: Overrides
    ) -> Effective:
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    def select_voice(self, *, model_id: str, voice_id: str) -> VoiceSelection:
        del model_id, voice_id
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    def export(self, narration_id: str, export_format: ExportFormat) -> None:
        del narration_id, export_format
        raise RuntimeError(ERROR_MESSAGES[ErrorCode.ENGINE_UNAVAILABLE])

    async def export_events(self) -> AsyncIterator[dict[str, object]]:
        if False:
            yield {}


def _history_summary(item: HistorySummary) -> dict[str, object]:
    return {
        "id": item.id,
        "sourcePreview": item.source_preview,
        "modelId": item.model_id,
        "voiceId": item.voice_id,
        "speed": item.speed,
        "status": item.status.value,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "lastPlayedAt": item.last_played_at.isoformat(),
        "playheadSec": item.playhead_sec,
        "totalDurationSec": item.total_duration_sec,
        "audioPresent": item.audio_present,
        "hasGaps": item.has_gaps,
    }


def _history_segment(part: HistorySegment) -> dict[str, object]:
    return {
        "ordinal": part.ordinal,
        "sourceStart": part.source_start,
        "sourceEnd": part.source_end,
        "boundary": part.boundary,
        "durationSec": part.duration_sec,
        "audioPresent": part.audio_present,
        "timelineStartSec": part.timeline_start_sec,
        "timings": [
            {
                "sourceStart": word.source_start,
                "sourceEnd": word.source_end,
                "startSec": word.start_sec,
                "endSec": word.end_sec,
                "provenance": word.provenance,
            }
            for word in part.timings
        ],
    }


def _history_gap(gap: HistoryGap) -> dict[str, object]:
    return {
        "ordinal": gap.ordinal,
        "sourceStart": gap.source_start,
        "sourceEnd": gap.source_end,
        "errorCode": gap.error_code,
        "createdAt": gap.created_at.isoformat(),
    }


def _history_detail(item: HistoryDetail, *, with_source: bool) -> dict[str, object]:
    payload = _history_summary(item)
    if with_source:
        payload["source"] = item.source
    payload["segments"] = [_history_segment(part) for part in item.segments]
    payload["gaps"] = [_history_gap(gap) for gap in item.gaps]
    return payload


def _attribution_on_the_wire(entry: PinnedArtifact) -> dict[str, object] | None:
    obligations = LICENCE_OBLIGATIONS[entry.license]
    reader_attribution = obligations.reader_attribution
    if reader_attribution is None:
        return None
    assert entry.attribution is not None
    return {
        "creator": entry.attribution.creator,
        "copyrightNotice": entry.attribution.copyright_notice,
        "source": entry.source.page_url,
        "warrantyNotice": reader_attribution.warranty_notice,
        "modified": entry.attribution.modified,
    }


def _reference_licences_on_the_wire(entry: CatalogEntry) -> list[dict[str, object]]:
    """The licences the entry's corpus-cut reference clips are under, each
    once with the clips credited beneath it, in the order the Voices come."""
    blocks: dict[str, dict[str, object]] = {}
    for voice in entry.voices:
        credit = voice.reference.attribution if voice.reference else None
        if credit is None:
            continue
        obligations = LICENCE_OBLIGATIONS[credit.license]
        assert obligations.reader_attribution is not None
        block = blocks.setdefault(
            credit.license,
            {
                "id": credit.license,
                "name": obligations.display_name,
                "text": licence_text(credit.license),
                "warrantyNotice": obligations.reader_attribution.warranty_notice,
                "clips": [],
            },
        )
        clips = block["clips"]
        assert isinstance(clips, list)
        clips.append(
            {
                "voice": voice.id,
                "creator": credit.creator,
                "copyrightNotice": credit.copyright_notice,
                "source": str(credit.source),
                "modified": credit.modified,
            }
        )
    return list(blocks.values())


def _licence_on_the_wire(entry: PinnedArtifact) -> dict[str, object]:
    obligations = LICENCE_OBLIGATIONS[entry.license]
    return {
        "id": entry.license,
        "name": obligations.display_name,
        "bindsReader": obligations.binds_reader,
        "credit": obligations.credit,
        "attribution": _attribution_on_the_wire(entry),
        "text": licence_text_for(entry),
    }


def create_app(
    token: str,
    narrator: Narrator | None = None,
    history: History | None = None,
    store: Store | None = None,
    downloads: Downloads | None = None,
    catalog: Manifest | None = None,
    backends: frozenset[Backend] | None = None,
    export_formats: tuple[ExportFormat, ...] | None = None,
    fetch_link: Callable[[str], Page] = fetch_page,
) -> FastAPI:
    """Build the Engine app with every route behind the launch token and
    the webview-only Origin allowlist. `backends` is what this machine can
    run, asked of the installed runtimes unless a test pins it,
    `export_formats` what it can export, its default first, and
    `fetch_link` how a link the reader opens is fetched, which tests fake so
    they never reach the network."""
    narrator = narrator or UnavailableNarrator()
    history = history or UnavailableHistory()
    # The Manifest is baked into the release, so the picker's data source
    # answers offline with no models downloaded (ADR 0003 §1). It carries
    # what a user chooses between — never the curation facts (hashes,
    # revisions, Backend) the Engine fetches and runs with. Injectable
    # because the bundling slice passes the app resource's path, and because
    # constructing the app should not be a hidden filesystem read.
    if catalog is None:
        catalog = load_manifest()
    if store is None:
        store = ModelStore(default_data_dir())
    if downloads is None:
        downloads = DownloadManager(store, fetch_entry)
    if backends is None:
        backends = available_backends()
    if export_formats is None:
        export_formats = tuple(export_encoders())
    default = default_here(catalog, backends)
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError):
        return error_response(422, ErrorCode.INVALID_REQUEST)

    @app.exception_handler(StarletteHTTPException)
    async def route_error(_request: Request, error: StarletteHTTPException):
        if isinstance(error.detail, ErrorCode):
            return error_response(error.status_code, error.detail)
        # `internal_error` is bound to 500 by docs/wire.md, so a routing
        # refusal must not borrow it. Starlette's `Allow` header is carried
        # through, so the answer stays a usable 405 rather than a riddle.
        if error.status_code == 405:
            response = error_response(405, ErrorCode.METHOD_NOT_ALLOWED)
            allow = (error.headers or {}).get("Allow")
            if allow is not None:
                response.headers["Allow"] = allow
            return response
        if error.status_code == 404:
            return error_response(404, ErrorCode.NOT_FOUND)
        return error_response(error.status_code, ErrorCode.INTERNAL_ERROR)

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _error: Exception):
        return error_response(500, ErrorCode.INTERNAL_ERROR)

    # Middleware runs last-added-first: token 401 before origin 403, so an
    # unauthenticated actual request learns nothing about the origin policy.
    # CORS is outermost so the browser's credential-free OPTIONS preflight can
    # complete; a preflight invokes no route and carries no user data.
    app.add_middleware(OriginAllowlistMiddleware, allowed=allowed_origins())
    app.add_middleware(BearerTokenMiddleware, token=token)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.decode() for origin in allowed_origins()],
        allow_methods=["DELETE", "GET", "OPTIONS", "PATCH", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(
        advanced_router(
            narrator=narrator,
            history=history,
            store=store,
            catalog=catalog,
            backends=backends,
        )
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def timing_choices(entry: CatalogEntry, voice_id: str | None = None) -> set[str]:
        """The Support Model each Voice's saved controls select, `off` for none."""
        voices = entry.voices if voice_id is None else (entry.voice(voice_id),)
        return {
            history.effective_controls(entry, voice.id).entry.timing_choice(voice.id)
            for voice in voices
        }

    def ready(entry: CatalogEntry, choices: set[str]) -> bool:
        return all(
            store.installed(artifact)
            for artifact in catalog.required_artifacts(entry, choices)
        )

    def installed(entry: CatalogEntry, voice_id: str | None = None) -> bool:
        return ready(entry, timing_choices(entry, voice_id))

    def download_bytes(entry: CatalogEntry) -> int:
        return sum(
            artifact.download_bytes
            for artifact in catalog.required_artifacts(entry, timing_choices(entry))
        )

    def picker_entry(entry: CatalogEntry) -> dict[str, object]:
        support_models = catalog.required_support(timing_choices(entry))
        return {
            "id": entry.id,
            "name": entry.display_name,
            "tier": entry.tier.value,
            # `license` is the id string v1 has always carried; the licence in
            # full arrived later and v1 shapes only grow (docs/wire.md).
            "license": entry.license,
            "licenseTerms": _licence_on_the_wire(entry),
            "referenceLicenses": _reference_licences_on_the_wire(entry),
            "supportModels": [
                {
                    "name": support.display_name,
                    "licenseTerms": _licence_on_the_wire(support),
                }
                for support in support_models
            ],
            "ramClassGb": entry.ram_class_gb
            + sum(support.ram_class_gb for support in support_models),
            "voices": [
                {
                    "id": voice.id,
                    "name": voice.name,
                    "language": voice.language,
                    "preview": voice.preview,
                    "simple": qualified(entry, voice.id) is not None,
                }
                for voice in entry.voices
            ],
            "defaultVoiceId": entry.default_voice,
            "downloadBytes": download_bytes(entry),
            "runsHere": runs_here(entry, backends),
            "wordTimingModels": [
                {"id": support.id, "name": support.display_name}
                for support in catalog.support_models
                if "word_timing" in entry.parameters
                and support.id in entry.parameters["word_timing"].choices
            ],
            "parameters": {
                name: declaration.model_dump(mode="json", by_alias=True)
                for name, declaration in entry.control_schema.items()
            },
            "effectiveValues": {
                voice.id: history.effective_controls(entry, voice.id).values
                for voice in entry.voices
            },
        }

    @app.get("/v1/catalog")
    def catalog_listing() -> dict[str, object]:
        return {
            "version": WIRE_VERSION,
            "defaultModelId": default.id,
            "defaultFastModelId": (
                None
                if catalog.default_fast_entry is None
                else catalog.default_fast_entry.id
            ),
            "models": [picker_entry(entry) for entry in catalog.models],
        }

    @app.get("/v1/models")
    def models_listing() -> dict[str, object]:
        statuses = [
            {
                "id": entry.id,
                "installed": installed(entry),
                "diskBytes": store.disk_usage(entry),
                "downloadBytes": download_bytes(entry),
            }
            for entry in catalog.models
        ]
        return {
            "version": WIRE_VERSION,
            "diskBytesTotal": sum(status["diskBytes"] for status in statuses)
            + sum(store.disk_usage(support) for support in catalog.support_models),
            "models": statuses,
        }

    @app.post("/v1/models/{model_id}/download", status_code=202)
    def download_model(model_id: str):
        entry = catalog.resolve(model_id)
        if entry is None:
            return error_response(404, ErrorCode.UNKNOWN_MODEL)
        if not runs_here(entry, backends):
            return error_response(409, ErrorCode.MODEL_UNSUPPORTED)
        downloads.start(entry, support=catalog.required_support(timing_choices(entry)))
        return {"version": WIRE_VERSION, "modelId": entry.id, "status": "accepted"}

    @app.delete("/v1/models/{model_id}")
    def delete_model(model_id: str, response: Response):
        entry = catalog.resolve(model_id)
        if entry is None:
            return error_response(404, ErrorCode.UNKNOWN_MODEL)
        outcome = downloads.delete(entry)
        if outcome == "queued":
            # The delete waits behind the job running and happens later.
            response.status_code = 202
        return {
            "version": WIRE_VERSION,
            "modelId": entry.id,
            "deleted": outcome == "deleted",
            "queued": outcome == "queued",
        }

    @app.delete("/v1/models/{model_id}/queue")
    def withdraw_model(model_id: str):
        entry = catalog.resolve(model_id)
        if entry is None:
            return error_response(404, ErrorCode.UNKNOWN_MODEL)
        removed = downloads.withdraw(entry)
        return {"version": WIRE_VERSION, "modelId": entry.id, "removed": removed}

    @app.get("/v1/models/events")
    def download_events() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            yield "retry: 1000\n\n"
            event_id = 0
            async for snapshot in downloads.events():
                event_id += 1
                yield sse_event(event_id, snapshot, event="download")

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/sources/fetch")
    def fetch_source(body: SourceFetchRequest) -> Response:
        try:
            page = fetch_link(body.url)
        except LinkError as error:
            return error_response(*LINK_ERRORS[error.kind])
        # Set as a header rather than `media_type`, which would add a
        # UTF-8 charset the page never named and hide its own <meta> one.
        return Response(page.body, headers={"Content-Type": page.content_type})

    @app.post("/v1/audio/speech", status_code=202)
    def speech(body: SpeechRequest):
        entry = default if body.model is None else catalog.resolve(body.model)
        if entry is None:
            return error_response(404, ErrorCode.UNKNOWN_MODEL)
        voice = body.voice if body.voice is not None else entry.default_voice
        if voice not in {offered.id for offered in entry.voices}:
            return error_response(422, ErrorCode.INVALID_REQUEST)
        # A model this build cannot load may still be on disk, left by the
        # Apple-silicon build in a data directory the Intel build now opens,
        # so this is asked before the disk is: telling the client to
        # download what it already has would send it round in a circle.
        if not runs_here(entry, backends):
            return error_response(409, ErrorCode.MODEL_UNSUPPORTED)
        # The Engine loads only store-promoted paths (ADR 0003 §3), so a
        # model that is not installed cannot be narrated with — tell the
        # client to download it rather than failing on the event stream.
        if not installed(entry, voice):
            return error_response(409, ErrorCode.MODEL_NOT_INSTALLED)
        try:
            narration_id = narrator.start(
                body.model_copy(update={"model": entry.id, "voice": voice})
            )
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        return {
            "version": WIRE_VERSION,
            "narrationId": narration_id,
            "status": "accepted",
        }

    @app.post("/v1/audio/stop")
    def stop():
        return {"version": WIRE_VERSION, "stopped": narrator.stop()}

    @app.patch("/v1/settings/playback")
    def playback_settings(body: PlaybackSettingsRequest):
        try:
            narrator.set_speed(body.speed)
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        return {"version": WIRE_VERSION, "speed": body.speed}

    @app.post("/v1/audio/pause")
    def pause():
        return {"version": WIRE_VERSION, "paused": narrator.pause()}

    @app.post("/v1/audio/play")
    def play():
        return {"version": WIRE_VERSION, "playing": narrator.play()}

    @app.post("/v1/audio/seek")
    def seek(body: SeekRequest | TimeSeekRequest):
        seeked = (
            narrator.seek(body.sourceOffset)
            if isinstance(body, SeekRequest)
            else narrator.seek_time(body.positionSec)
        )
        return {"version": WIRE_VERSION, "seeked": seeked}

    @app.get("/v1/history")
    def history_listing() -> dict[str, object]:
        return {
            "version": WIRE_VERSION,
            "history": [_history_summary(item) for item in history.list()],
        }

    def retention_payload(state: RetentionState) -> dict[str, object]:
        return {
            "version": WIRE_VERSION,
            "segmentBudgetBytes": state.segment_budget_bytes,
            "keepAudioDays": state.keep_audio_days,
            "diskUsage": {
                "modelsBytes": sum(store.disk_usage(entry) for entry in catalog.models),
                "audioBytes": state.audio_bytes,
            },
        }

    @app.get("/v1/settings/retention")
    def retention_settings():
        try:
            return retention_payload(history.retention())
        except HistorySchemaError:
            raise
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)

    @app.patch("/v1/settings/retention")
    def update_retention_settings(body: RetentionRequest):
        try:
            applied = history.update_retention(
                segment_budget_bytes=body.segmentBudgetBytes,
                keep_audio_days=body.keepAudioDays,
            )
        except HistorySchemaError:
            raise
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        # The one field `GET` cannot have: what applying *this* policy took.
        # Only the sweep knows it, and a client subtracting two disk
        # readings would be crediting the Janitor's own sweeps to whatever
        # the reader last pressed.
        return retention_payload(applied.state) | {
            "evictedBytes": applied.evicted_bytes,
        }

    def selection_payload(selection: VoiceSelection) -> dict[str, object]:
        """The stored choice as the picker can use it.

        Resolved rather than echoed: a release can retire a Catalog entry
        out from under a stored id, and a picker told to render a Voice
        Model that no longer exists has nothing to draw. Nor can it draw
        one this machine cannot run, which the Apple-silicon build can
        leave in a data directory the Intel build then opens. Falling back
        to this machine's default is the same answer a fresh install gets.
        """
        entry = (
            None if selection.model_id is None else catalog.resolve(selection.model_id)
        )
        if entry is None or not runs_here(entry, backends):
            entry = default
        offered = {voice.id for voice in entry.voices}
        voice = (
            selection.voice_id if selection.voice_id in offered else entry.default_voice
        )
        return {"version": WIRE_VERSION, "modelId": entry.id, "voiceId": voice}

    @app.get("/v1/settings/voice")
    def voice_settings():
        try:
            return selection_payload(history.voice_selection())
        except HistorySchemaError:
            raise
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)

    @app.patch("/v1/settings/voice")
    def update_voice_settings(body: VoiceRequest):
        entry = resolve_voice(catalog, body.modelId, body.voiceId)
        try:
            stored = history.select_voice(model_id=entry.id, voice_id=body.voiceId)
        except HistorySchemaError:
            raise
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        return selection_payload(stored)

    @app.get("/v1/history/{narration_id}")
    def history_detail(
        narration_id: str, afterOrdinal: Annotated[int | None, Query(ge=0)] = None
    ):
        item = history.detail(narration_id, after_ordinal=afterOrdinal)
        if item is None:
            return error_response(404, ErrorCode.NOT_FOUND)
        return {
            "version": WIRE_VERSION,
            **_history_detail(item, with_source=afterOrdinal is None),
        }

    @app.delete("/v1/history/{narration_id}")
    def delete_history(narration_id: str):
        try:
            result = history.delete(narration_id)
        except HistorySchemaError:
            raise
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        if result is None:
            return error_response(404, ErrorCode.NOT_FOUND)
        return {
            "version": WIRE_VERSION,
            "narrationId": result.narration_id,
            "deleted": True,
            "audioBytesFreed": result.audio_bytes_freed,
        }

    @app.post("/v1/history/{narration_id}/resume", status_code=202)
    def resume_history(narration_id: str, paused: bool = False):
        item = history.detail(narration_id)
        if item is None:
            return error_response(404, ErrorCode.NOT_FOUND)
        refusal = regeneration_refusal(store, catalog, backends, item)
        if refusal is not None:
            return error_response(409, refusal)
        try:
            resumed = history.resume(narration_id, paused=paused)
        except NarrationNotResumable:
            return error_response(409, ErrorCode.NARRATION_NOT_RESUMABLE)
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        return {
            "version": WIRE_VERSION,
            "narrationId": resumed,
            "status": "accepted",
        }

    @app.post("/v1/history/{narration_id}/export", status_code=202)
    def export_history(narration_id: str, body: ExportRequest):
        """Accept an Export and answer immediately.

        Accepted work, not finished work: a Narration whose audio a
        retention sweep has evicted has to be re-synthesized first, and
        that queues behind whatever is playing. Progress is on
        `/v1/export/events`; pressing the button costs the same either way.
        """
        export_format = body.format or export_formats[0]
        if export_format not in export_formats:
            return error_response(422, ErrorCode.INVALID_REQUEST)
        item = history.detail(narration_id)
        if item is None:
            return error_response(404, ErrorCode.NOT_FOUND)
        # Only when a Block would actually have to be synthesized: an Export
        # that reads entirely from the lossless Segment cache needs no model
        # at all, so it must not be refused for one the user has since
        # deleted (ADR 0004 §3) or one this build cannot load. Recorded gaps
        # are not missing audio — they are Blocks Export replays as gaps, so
        # they never need a model.
        gaps = {gap.ordinal for gap in item.gaps}
        needs_synthesis = any(
            not part.audio_present and part.ordinal not in gaps
            for part in item.segments
        )
        refusal = (
            regeneration_refusal(store, catalog, backends, item)
            if needs_synthesis
            else None
        )
        if refusal is not None:
            return error_response(409, refusal)
        try:
            history.export(narration_id, export_format)
        except KeyError:
            return error_response(404, ErrorCode.NOT_FOUND)
        except ExportInProgress:
            return error_response(409, ErrorCode.EXPORT_IN_PROGRESS)
        except HistorySchemaError:
            raise
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        return {
            "version": WIRE_VERSION,
            "narrationId": narration_id,
            "format": export_format,
            "status": "accepted",
        }

    @app.get("/v1/export/events")
    def export_events() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            yield "retry: 1000\n\n"
            event_id = 0
            async for snapshot in history.export_events():
                event_id += 1
                yield sse_event(event_id, snapshot, event="export")

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/events")
    def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            yield "retry: 1000\n\n"
            event_id = 0
            async for snapshot in narrator.events():
                event_id += 1
                yield sse_event(event_id, snapshot)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
