"""VoxCPM2 on the MLX lane (threat model B2).

The Architecture owns how the record reaches mlx-audio's VoxCPM2 class; the
lane owns everything downstream of the call. A Voice is cloned from its
Voice Reference at every Block, so the speaker holds across paragraphs;
zero-shot VoxCPM2 draws a new one per generation. Every Block uses
upstream's Hi-Fi cloning: the clip conditions the voice as a reference and
also, with its transcript, as the prompt the Block continues from.

mlx-audio encodes a reference by resampling it to the audio VAE's 16 kHz
with SciPy, which ADR 0006 keeps out of the shipped Engine. `load` swaps
that step for `encode_reference`, and `generate` hands the clip over already
at 16 kHz, resampled by the Engine's own polyphase filter.
"""

import re
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from readily_engine.audio import FloatPcm
from readily_engine.audio.resampling import resample_poly
from readily_engine.catalog import CatalogEntry
from readily_engine.generation import GenerationRecord, Synthesizer
from readily_engine.loading.mlx_lane import (
    GenerationResult,
    MlxSynthesizer,
    runaway_budget_seconds,
)
from readily_engine.loading.references import (
    REFERENCE_SAMPLE_RATE,
    VoiceReferenceAudio,
    VoiceReferences,
)

# The audio VAE's input rate, where a reference is encoded; output is 48 kHz.
ENCODER_SAMPLE_RATE = 16_000

# One patch is four 25 Hz latent frames (16 kHz in, 640-sample hop; 48 kHz
# out, 1920-sample hop). Keep upstream's 2000-patch default as the ceiling
# for unusually long inputs.
PATCHES_PER_SECOND = 6.25
MAX_TOKENS = 2000

# What mlx-audio's loader reads; the tokenizer sidecars the entry also pins
# are read through it and named by the Catalog, not here.
EXPECTED_FILES = frozenset({"config.json", "model.safetensors", "tokenizer.json"})


# The diffusion knobs mlx-audio's VoxCPM2 `generate` takes; one left unset
# keeps upstream's default.
class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    cfg_value: float | None = Field(default=None, gt=0)
    inference_timesteps: int | None = Field(default=None, gt=0, strict=True)


class AudioVae(Protocol):
    chunk_size: int

    def encode(self, audio: object, sample_rate: int) -> object: ...


class VoxCpm2Model(Protocol):
    patch_size: int
    audio_vae: AudioVae

    def generate(
        self,
        text: str,
        *,
        ref_audio: object,
        prompt_audio: object,
        prompt_text: str,
        max_tokens: int,
        **parameters: float | int,
    ) -> Iterator[GenerationResult]: ...


def token_budget(text: str) -> int:
    """The patch ceiling a Block for `text` decodes to: the lane's runaway
    budget in patches, capped at upstream's default."""
    return min(MAX_TOKENS, int(runaway_budget_seconds(text) * PATCHES_PER_SECOND))


def runaway_seconds(text: str) -> float:
    """The runaway budget in seconds of the patch ceiling VoxCPM2 decodes to."""
    return token_budget(text) / PATCHES_PER_SECOND


def encode_reference(
    model: VoxCpm2Model,
    audio: FloatPcm,
    padding_mode: str = "right",
    trim_silence_vad: bool = False,
):
    """mlx-audio's `_encode_wav`, with its signature, for audio already at the
    encoder's rate: pad to whole patches, VAE-encode, and group the frames
    into patches."""
    import mlx.core as mx

    if trim_silence_vad:
        raise NotImplementedError("VoxCPM2 references are trimmed at curation")
    audio = np.asarray(audio, dtype=np.float32).ravel()
    patch_samples = model.patch_size * model.audio_vae.chunk_size
    padding = -len(audio) % patch_samples
    audio = np.pad(audio, (padding, 0) if padding_mode == "left" else (0, padding))
    frames = model.audio_vae.encode(mx.array(audio)[None, None, :], ENCODER_SAMPLE_RATE)
    frames = frames.squeeze(0)
    patches = frames.shape[0] // model.patch_size
    return frames[: patches * model.patch_size].reshape(patches, model.patch_size, -1)


def generate(
    model: VoxCpm2Model,
    record: GenerationRecord,
    reference: VoiceReferenceAudio | None = None,
) -> Iterator[GenerationResult]:
    """Clone the record's Voice from its hash-verified Voice Reference and
    continue from it, with whitespace collapsed as upstream's `generate` does."""
    if reference is None:
        raise ValueError(f"VoxCPM2 Voice {record.voice_id!r} has no Voice Reference")
    clip = resample_poly(reference.pcm, REFERENCE_SAMPLE_RATE, ENCODER_SAMPLE_RATE)
    text = re.sub(r"\s+", " ", record.text)
    return model.generate(
        text,
        ref_audio=clip,
        prompt_audio=clip,
        prompt_text=reference.text,
        max_tokens=token_budget(record.text),
        **Parameters.model_validate(record.parameters).model_dump(exclude_none=True),
    )


def _load(model_dir: Path) -> VoxCpm2Model:
    from mlx_audio.tts.utils import load

    model = load(model_dir, lazy=True)
    model._encode_wav = partial(encode_reference, model)
    return model


class VoxCpm2Architecture:
    """VoxCPM2's Architecture as the registry names it (ADR 0014)."""

    backend = "mlx-audio"
    conditioning = "reference"
    warmup_text = "Ready, Zyntrix."
    parameters = Parameters
    chunk_budget_candidates = None

    def expected_files(self, entry: CatalogEntry) -> frozenset[str]:
        return EXPECTED_FILES

    def load(
        self, model_dir: Path, entry: CatalogEntry, *, references: VoiceReferences
    ) -> Synthesizer:
        return MlxSynthesizer(
            model_dir,
            generate=generate,
            runaway_seconds=runaway_seconds,
            references=references,
            model_factory=_load,
        )


ARCHITECTURE = VoxCpm2Architecture()
