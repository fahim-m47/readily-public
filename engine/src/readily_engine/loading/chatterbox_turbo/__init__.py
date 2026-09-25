"""Chatterbox Turbo on the MLX lane (threat model B2).

The Architecture owns how the record reaches mlx-audio's Chatterbox Turbo class:
its `generate` takes text and the decoder options only — the Voice is the
one baked into the promoted directory, never a name or a reference clip (ADR
0009 amendment). `MlxSynthesizer` owns everything downstream of the call.
Upstream ships Chatterbox and Chatterbox Turbo as different classes under
one family name, so this Architecture answers only to the `chatterbox_turbo` id.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from readily_engine.catalog import CatalogEntry
from readily_engine.generation import (
    GenerationRecord,
    Synthesizer,
)
from readily_engine.loading.mlx_lane import (
    STREAMING_INTERVAL_SECONDS,
    GenerationResult,
    MlxSynthesizer,
    runaway_budget_seconds,
)
from readily_engine.loading.references import VoiceReferenceAudio, VoiceReferences

# What mlx-audio's loader reads, plus the baked-in Voice's conditionals.
EXPECTED_FILES = frozenset({"config.json", "model.safetensors", "conds.safetensors"})


# The sampling knobs mlx-audio's Chatterbox Turbo `generate` takes; one
# left unset keeps upstream's default.
class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    repetition_penalty: float | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0)
    top_k: int | None = Field(default=None, ge=0, strict=True)
    top_p: float | None = Field(default=None, gt=0, le=1)


class ChatterboxTurboModel(Protocol):
    def generate(
        self,
        text: str,
        *,
        stream: bool,
        streaming_interval: float,
        **parameters: float | int,
    ) -> Iterator[GenerationResult]: ...


def generate(
    model: ChatterboxTurboModel,
    record: GenerationRecord,
    reference: VoiceReferenceAudio | None = None,
) -> Iterator[GenerationResult]:
    return model.generate(
        record.text,
        **Parameters.model_validate(record.parameters).model_dump(exclude_none=True),
        stream=record.decode_mode == "streaming",
        streaming_interval=STREAMING_INTERVAL_SECONDS,
    )


class ChatterboxTurboArchitecture:
    """Chatterbox Turbo's Architecture as the registry names it (ADR 0014)."""

    expected_files = EXPECTED_FILES
    conditioning = "preset"
    warmup_text = "Ready, Zyntrix."
    parameters = Parameters
    chunk_budget_candidates = None

    def load(
        self, model_dir: Path, entry: CatalogEntry, *, references: VoiceReferences
    ) -> Synthesizer:
        return MlxSynthesizer(
            model_dir, generate=generate, runaway_seconds=runaway_budget_seconds
        )


ARCHITECTURE = ChatterboxTurboArchitecture()
