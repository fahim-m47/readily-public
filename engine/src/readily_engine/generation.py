"""The frozen input to synthesis and the identity of its Segment."""

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

import numpy as np
from pydantic import AllowInfNan, Field, Strict, StrictInt, TypeAdapter

from readily_engine.audio import FloatPcm
from readily_engine.timings import Timing

if TYPE_CHECKING:
    from readily_engine.catalog import CatalogEntry


# The knobs an Architecture reads, by name. Which names and values are valid
# is the Architecture's schema to say (ADR 0014): `readily-curate --write` and
# Engine boot check an entry against it, and `generate` parses it again. Only
# the knobs an entry pins are present, so an entry pinning none has `{}`.
type Parameters = dict[str, StrictInt | Annotated[float, Strict(), AllowInfNan(False)]]


@dataclass(frozen=True)
class GenerationRecord:
    """Everything that determines one Block's audio, independent of its position.

    A null seed derives the draw from the complete record. An explicit seed
    requests a re-roll and is itself part of the Segment's identity.
    """

    model_id: str
    catalog_version: int
    voice_id: str
    reference_digest: str | None
    decode_mode: Literal["streaming", "non-streaming"]
    parameters: Parameters
    text: str
    seed: Annotated[int, Field(ge=0, le=2**32 - 1)] | None = None
    format: Literal[2] = 2
    word_timing: str = "off"

    @classmethod
    def for_entry(
        cls,
        entry: "CatalogEntry",
        voice_id: str,
        text: str,
        *,
        seed: int | None = None,
    ) -> "GenerationRecord":
        voice = entry.voice(voice_id)
        return cls(
            model_id=entry.id,
            catalog_version=entry.version,
            voice_id=voice_id,
            reference_digest=voice.reference.sha256 if voice.reference else None,
            decode_mode=entry.decode_mode,
            # A copy, so no change to a Record can reach the Catalog entry.
            parameters=dict(entry.generation_parameters),
            text=text.strip(),
            seed=seed,
            word_timing=entry.timing_choice(voice_id),
        )

    def canonical_json(self) -> str:
        fields = asdict(self)
        if self.word_timing == "off":
            fields.pop("word_timing")
        return json.dumps(
            fields,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, value: str) -> "GenerationRecord":
        return _RECORD_ADAPTER.validate_json(value)

    @property
    def key(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def rng_seed(self) -> int:
        return int(self.key[:8], 16) if self.seed is None else self.seed

    def redraw(self) -> "GenerationRecord":
        """The record the worker retries with: a different draw, still reproducible."""
        return replace(self, seed=(self.rng_seed + 1) % 2**32)


# Building the adapter costs far more than a validation, and History parses one
# record per Segment when it lists a page.
_RECORD_ADAPTER = TypeAdapter(GenerationRecord)


@dataclass(frozen=True)
class GeneratedAudio:
    """One Block's finished audio, refused at construction if it cannot be
    played, so no consumer has to check again."""

    pcm: FloatPcm
    sample_rate: int
    timings: tuple[Timing, ...] = ()
    cutoffs: int = 0

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("audio with a non-positive sample rate")
        if not len(self.pcm):
            raise ValueError("empty audio")
        if not np.isfinite(self.pcm).all():
            raise ValueError("audio with non-finite samples")


class DegenerateDraw(RuntimeError):
    """Every draw for one Block was rejected by the Synthesizer's own gate."""


class Synthesizer(Protocol):
    """Synthesize a complete Block."""

    def generate(self, record: GenerationRecord) -> GeneratedAudio: ...
