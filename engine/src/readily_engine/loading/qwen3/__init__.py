"""Qwen3-TTS on the MLX lane (threat model B2).

The Architecture owns how the record reaches mlx-audio's Qwen3 class and the token
budget measured on this model; `MlxSynthesizer` owns everything downstream
of the call (gate, scrub, runaway cutoff). A Voice conditions on its Voice
Reference clip at every Block: the Base checkpoint's speaker table is
empty, so a name draws a new speaker per generation. The clip is handed over
as an in-memory array so mlx-audio's own SciPy decoder is never reached
(ADR 0006). The Voice's language picks the codec's language id, which
mlx-audio's default "auto" leaves out.
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

# Qwen3's 12 Hz tokenizer produces 12.5 codec frames per second. Keep its
# upstream 4096-token ceiling for unusually long inputs.
CODEC_TOKENS_PER_SECOND = 12.5
MAX_TOKENS = 4096

# What mlx-audio's loader reads, the text tokenizer and generation config
# included: it carries on without either rather than failing.
EXPECTED_FILES = frozenset(
    {
        "config.json",
        "generation_config.json",
        "merges.txt",
        "model.safetensors",
        "speech_tokenizer/config.json",
        "speech_tokenizer/model.safetensors",
        "tokenizer_config.json",
        "vocab.json",
    }
)

# The names mlx-audio's `lang_code` takes for the languages Qwen3-TTS has a
# codec language id for, by BCP 47 primary subtag.
LANG_CODES = {
    "de": "german",
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "it": "italian",
    "ja": "japanese",
    "ko": "korean",
    "pt": "portuguese",
    "ru": "russian",
    "zh": "chinese",
}


# The sampling knobs mlx-audio's Qwen3-TTS `generate` takes; one left
# unset keeps upstream's default.
class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    repetition_penalty: float | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0)
    top_k: int | None = Field(default=None, ge=0, strict=True)
    top_p: float | None = Field(default=None, gt=0, le=1)


class SpeechTokenizer(Protocol):
    has_encoder: bool


class Qwen3Model(Protocol):
    speech_tokenizer: SpeechTokenizer

    def generate(
        self,
        text: str,
        *,
        voice: str | None = None,
        ref_audio: object = None,
        ref_text: str | None = None,
        stream: bool,
        streaming_interval: float,
        verbose: bool,
        max_tokens: int,
        lang_code: str,
        **parameters: float | int,
    ) -> Iterator[GenerationResult]: ...


def token_budget(text: str) -> int:
    """The codec-token ceiling a Block for `text` decodes to: the lane's
    runaway budget in tokens, capped at upstream's maximum."""
    return min(MAX_TOKENS, int(runaway_budget_seconds(text) * CODEC_TOKENS_PER_SECOND))


def runaway_seconds(text: str) -> float:
    """The runaway budget in seconds of the token ceiling Qwen decodes to."""
    return token_budget(text) / CODEC_TOKENS_PER_SECOND


def generate(
    model: Qwen3Model,
    record: GenerationRecord,
    reference: VoiceReferenceAudio | None = None,
    *,
    lang_code: str = "auto",
) -> Iterator[GenerationResult]:
    """Generate from the record and its hash-verified Voice Reference, in
    `lang_code` (one of `LANG_CODES`' names, or "auto")."""
    shared = {
        **Parameters.model_validate(record.parameters).model_dump(exclude_none=True),
        "stream": record.decode_mode == "streaming",
        # mlx-audio calls this max_tokens; max_new_tokens is silently ignored.
        "max_tokens": token_budget(record.text),
        "streaming_interval": STREAMING_INTERVAL_SECONDS,
        "verbose": False,
        "lang_code": lang_code,
    }
    if reference is None:
        return model.generate(record.text, voice=record.voice_id, **shared)
    # mlx-audio conditions on `ref_audio` only when the speech tokenizer loaded
    # its encoder, and otherwise narrates unconditioned without a word; a
    # checkpoint missing it is refused rather than drifting.
    if not model.speech_tokenizer.has_encoder:
        raise RuntimeError(
            "the promoted Qwen3-TTS checkpoint has no speech-tokenizer encoder, "
            f"so {record.voice_id}'s reference would be ignored"
        )
    # Passing an in-memory array avoids mlx-audio's SciPy decoder (ADR 0006).
    import mlx.core as mx

    return model.generate(
        record.text,
        ref_audio=mx.array(reference.pcm),
        ref_text=reference.text,
        **shared,
    )


class Qwen3Architecture:
    """Qwen3-TTS's Architecture as the registry names it (ADR 0014)."""

    backend = "mlx-audio"
    conditioning = "reference"
    warmup_text = "Ready, Zyntrix."
    parameters = Parameters
    # Simple-mode qualification sweeps this Block budget range; the
    # procedure is in engine/README.md.
    chunk_budget_candidates = range(200, 251)

    def expected_files(self, entry: CatalogEntry) -> frozenset[str]:
        return EXPECTED_FILES

    def load(
        self, model_dir: Path, entry: CatalogEntry, *, references: VoiceReferences
    ) -> Synthesizer:
        lang_codes = {
            voice.id: LANG_CODES.get(voice.language.split("-")[0].lower(), "auto")
            for voice in entry.voices
        }

        def generate_in_language(
            model: Qwen3Model,
            record: GenerationRecord,
            reference: VoiceReferenceAudio | None,
        ) -> Iterator[GenerationResult]:
            return generate(
                model, record, reference, lang_code=lang_codes[record.voice_id]
            )

        return MlxSynthesizer(
            model_dir,
            generate=generate_in_language,
            runaway_seconds=runaway_seconds,
            references=references,
        )


ARCHITECTURE = Qwen3Architecture()
