"""Naming a Catalog entry on the wire, and answering whether it can run.

The base app and the Advanced-mode router both let a request name a Voice,
and both refuse an unknown one the same way, so the request shape and the
two resolutions live here rather than in either module.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from readily_engine.catalog import CatalogEntry, Manifest, PinnedArtifact
from readily_engine.server.wire import ErrorCode
from readily_engine.storage.storage import HistoryDetail


class Store(Protocol):
    """What HTTP needs from the model store; the invariant lives in
    `readily_engine.store` (B1), not here."""

    def installed(self, entry: PinnedArtifact) -> bool: ...

    def disk_usage(self, entry: PinnedArtifact) -> int: ...


class VoiceRequest(BaseModel):
    """The Voice a user picked, named the way every other model route names
    one: a `name:tag` or a bare `name` that resolves to its default tag,
    plus a voice the resolved entry actually offers."""

    model_config = ConfigDict(extra="forbid")

    modelId: str = Field(min_length=1, max_length=256)
    voiceId: str = Field(min_length=1, max_length=256)


def resolve_voice(catalog: Manifest, model_id: str, voice_id: str) -> CatalogEntry:
    """Answer the Catalog entry a settings route names, or refuse the request."""
    entry = catalog.resolve(model_id)
    if entry is None:
        raise StarletteHTTPException(404, detail=ErrorCode.UNKNOWN_MODEL)
    if voice_id not in {voice.id for voice in entry.voices}:
        raise StarletteHTTPException(422, detail=ErrorCode.INVALID_REQUEST)
    return entry


def history_installed(
    store: Store, catalog: Manifest, entry: CatalogEntry, item: HistoryDetail
) -> bool:
    """Regeneration uses frozen Block controls, never today's Voice settings."""
    gaps = {gap.ordinal for gap in item.gaps}
    choices = {
        part.word_timing
        for part in item.segments
        if not part.audio_present
        and part.ordinal not in gaps
        and part.word_timing is not None
    }
    return all(
        store.installed(artifact)
        for artifact in catalog.required_artifacts(entry, choices)
    )
