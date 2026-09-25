"""Advanced mode's routes: one Voice's Controls, and one Block's takes.

Mounted on the same app as everything else, so the launch token and the
webview-only Origin allowlist admit these routes too (threat model B3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from readily_engine.catalog import CatalogEntry, Manifest
from readily_engine.catalog.controls import Overrides
from readily_engine.server.entries import (
    Store,
    VoiceRequest,
    history_installed,
    resolve_voice,
)
from readily_engine.server.wire import WIRE_VERSION, ErrorCode, error_response
from readily_engine.storage.history import HistorySchemaError

if TYPE_CHECKING:
    from readily_engine.server.app import History, Narrator


class ControlsRequest(VoiceRequest):
    """Replace one Voice's overrides; an empty record restores Manifest defaults."""

    overrides: Overrides = Field(max_length=32)


class TakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ordinal: int = Field(strict=True, ge=0, le=1_000_000)
    action: Literal["reroll", "A", "B"]


def controls_payload(
    entry: CatalogEntry, voice_id: str, overrides: Overrides
) -> dict[str, object]:
    """Shape one Voice's Controls for the wire.

    Both controls routes answer through here so that a Manifest which has
    dropped a control cannot make a read and a write disagree about what
    the Voice now resolves to.
    """
    return {
        "version": WIRE_VERSION,
        "modelId": entry.id,
        "voiceId": voice_id,
        "overrides": overrides,
        "effectiveValues": entry.compose(overrides).values,
    }


def advanced_router(
    *,
    narrator: Narrator,
    history: History,
    store: Store,
    catalog: Manifest,
) -> APIRouter:
    """Build the Advanced-mode routes over the app's own dependencies."""
    router = APIRouter()

    @router.get("/v1/settings/controls")
    def controls(modelId: str, voiceId: str):
        entry = resolve_voice(catalog, modelId, voiceId)
        try:
            overrides = history.control_overrides(entry, voiceId)
        except HistorySchemaError:
            raise
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        return controls_payload(entry, voiceId, overrides)

    @router.patch("/v1/settings/controls")
    def update_controls(body: ControlsRequest):
        entry = resolve_voice(catalog, body.modelId, body.voiceId)
        try:
            history.set_control_overrides(entry, body.voiceId, body.overrides)
        except ValueError:
            return error_response(422, ErrorCode.INVALID_REQUEST)
        except HistorySchemaError:
            raise
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        return controls_payload(entry, body.voiceId, body.overrides)

    @router.post("/v1/history/{narration_id}/take", status_code=202)
    def select_take(narration_id: str, body: TakeRequest):
        item = history.detail(narration_id)
        if item is None:
            return error_response(404, ErrorCode.NOT_FOUND)
        entry = catalog.resolve(item.model_id)
        if entry is None or not history_installed(store, catalog, entry, item):
            return error_response(409, ErrorCode.MODEL_NOT_INSTALLED)
        try:
            accepted = narrator.select_take(narration_id, body.ordinal, body.action)
        except HistorySchemaError:
            raise
        except RuntimeError:
            return error_response(503, ErrorCode.ENGINE_UNAVAILABLE)
        if not accepted:
            return error_response(422, ErrorCode.INVALID_REQUEST)
        return {
            "version": WIRE_VERSION,
            "narrationId": narration_id,
            "status": "accepted",
        }

    return router
