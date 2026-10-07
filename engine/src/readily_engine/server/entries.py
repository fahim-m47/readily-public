"""Naming a Catalog entry on the wire, and answering whether it can run.

The base app and the Advanced-mode router both let a request name a Voice,
and both refuse an unknown one the same way, so the request shape and the
two resolutions live here rather than in either module.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from readily_engine.catalog import CatalogEntry, Manifest, PinnedArtifact
from readily_engine.loading.architecture import Backend
from readily_engine.loading.registry import architecture_named
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


def runs_here(entry: CatalogEntry, backends: frozenset[Backend]) -> bool:
    """Whether this machine has the Backend the entry's Architecture runs on.

    The Backend itself stays off the wire (CONTEXT.md); the picker needs
    only the answer.
    """
    return architecture_named(entry.architecture).backend in backends


def default_here(catalog: Manifest, backends: frozenset[Backend]) -> CatalogEntry:
    """What a fresh install narrates with on this machine.

    The Manifest's default, unless its Backend is not here: the Intel build
    has no MLX, and a default it cannot run would stall the first run on a
    download the Engine itself refuses. Then the fast model stands in, and
    the Manifest's default is the last resort when neither runs.
    """
    default = catalog.default_entry
    if runs_here(default, backends):
        return default
    fast = catalog.default_fast_entry
    if fast is not None and runs_here(fast, backends):
        return fast
    return default


def regeneration_refusal(
    store: Store,
    catalog: Manifest,
    backends: frozenset[Backend],
    item: HistoryDetail,
) -> ErrorCode | None:
    """Why a Narration's stored Voice Model cannot synthesize again, or None.

    Whether the machine can run it is asked before whether it is on disk:
    the Apple-silicon build can fill a data directory the Intel build then
    opens, and telling that build to download what it already has would
    send it round in a circle. Regeneration uses frozen Block controls,
    never today's Voice settings.
    """
    entry = catalog.resolve(item.model_id)
    if entry is None:
        return ErrorCode.MODEL_NOT_INSTALLED
    if not runs_here(entry, backends):
        return ErrorCode.MODEL_UNSUPPORTED
    gaps = {gap.ordinal for gap in item.gaps}
    choices = {
        part.word_timing
        for part in item.segments
        if not part.audio_present
        and part.ordinal not in gaps
        and part.word_timing is not None
    }
    if not all(
        store.installed(artifact)
        for artifact in catalog.required_artifacts(entry, choices)
    ):
        return ErrorCode.MODEL_NOT_INSTALLED
    return None
