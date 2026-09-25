"""Pin Simple-mode qualification to the complete recipe, independent of text."""

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from readily_engine.generation import GenerationRecord

if TYPE_CHECKING:
    from readily_engine.catalog import CatalogEntry
    from readily_engine.catalog.manifest import Effective

Mode = Literal["simple", "advanced"]


class Qualification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UnqualifiedRecipe(ValueError):
    """The requested inputs have not passed Simple-mode qualification."""


def recipe_digest(entry: "CatalogEntry", voice_id: str) -> str:
    """Bind qualification to generation, assembly and chunking."""
    record = json.loads(
        GenerationRecord.for_entry(entry, voice_id, "").canonical_json()
    )
    record.pop("word_timing", None)
    value = {
        "record": record,
        "tunables": entry.tunables.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def qualified(entry: "CatalogEntry", voice_id: str) -> Qualification | None:
    """A changed default invalidates qualification until a curator checks it again."""
    voice = next((voice for voice in entry.voices if voice.id == voice_id), None)
    if voice is None or voice.qualification is None:
        return None
    result = voice.qualification
    if result.recipe_sha256 != recipe_digest(entry, voice_id):
        return None
    return result


def resolve_simple(entry: "CatalogEntry", voice_id: str) -> "Effective":
    """Resolve a qualified preset without reading or rewriting Advanced overrides."""
    if qualified(entry, voice_id) is None:
        raise UnqualifiedRecipe(
            "This Voice has no qualified Simple-mode recipe. Use Advanced."
        )
    return entry.compose({})
