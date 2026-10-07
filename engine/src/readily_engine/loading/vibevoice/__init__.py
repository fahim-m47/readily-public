"""VibeVoice-Realtime on the MLX lane (threat model B2).

Microsoft's streaming 0.5B model narrates English in one of the preset
Voices its checkpoint ships as prefilled prompt caches under `voices/`; it
takes no Voice Reference (upstream embeds its voice prompts "to mitigate
deepfake risks"). Its weights are MIT; Microsoft's model card recommends
research use, and the Catalog entry's provenance note says what that means.

mlx-audio's `post_load_hook` fetches the Qwen2.5 tokenizer from the Hub, so
this block loads through `load_without_hook` and attaches the copy bundled
under `tokenizer/`, refused unless its bytes match the digests pinned here.

The text reaching the model is normalised the way upstream's realtime demo
does it (curly quotes to straight ones): the tokenizer spells them as rare
byte pieces the model barely saw in training.
"""

# Curly quotes are the point of the normalisation table.
# ruff: noqa: RUF001

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from readily_engine.catalog import CatalogEntry
from readily_engine.generation import GenerationRecord, Synthesizer
from readily_engine.loading.mlx_lane import (
    STREAMING_INTERVAL_SECONDS,
    GenerationResult,
    MlxSynthesizer,
    load_without_hook,
    runaway_budget_seconds,
)
from readily_engine.loading.references import VoiceReferenceAudio, VoiceReferences

# Each speech latent decodes to 3200 samples at 24 kHz.
LATENTS_PER_SECOND = 7.5

# What mlx-audio's loader reads for every entry; `expected_files` adds each
# Voice's prompt cache under `voices/`.
EXPECTED_FILES = frozenset({"config.json", "model.safetensors"})

TOKENIZER_DIRECTORY = Path(__file__).parent / "tokenizer"
_TOKENIZER_HASHES = {
    "merges.txt": "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3",
    "tokenizer_config.json": (
        "c91efca15ceff6e9ee9424db58a6f59cd41294e550a86cbd07e3c1fb500b34f9"
    ),
    "vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
}

_TOKENIZER_DOCS = frozenset({"LICENSE", "README.md"})

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def verify_bundled_tokenizer() -> None:
    """Refuse an altered bundled tokenizer before transformers parses it."""
    # A stray file such as tokenizer.json would win over the pinned ones;
    # hidden files (Finder's .DS_Store) are never read, so they may stay.
    present = {
        path.name
        for path in TOKENIZER_DIRECTORY.iterdir()
        if not path.name.startswith(".")
    } - _TOKENIZER_DOCS
    if present != _TOKENIZER_HASHES.keys():
        raise ValueError(f"VibeVoice tokenizer files differ: {sorted(present)}")
    for name, expected in _TOKENIZER_HASHES.items():
        with (TOKENIZER_DIRECTORY / name).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"VibeVoice tokenizer hash mismatch: {name}")


# The knobs mlx-audio's VibeVoice `generate` takes; one left unset keeps
# upstream's default (guidance 1.5, the config's 20 diffusion steps).
class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    cfg_scale: float | None = Field(default=None, ge=1)
    ddpm_steps: int | None = Field(default=None, gt=0, strict=True)


class VibeVoiceModel(Protocol):
    tokenizer: object

    def generate(
        self,
        text: str,
        *,
        voice: str,
        max_tokens: int,
        stream: bool,
        streaming_interval: float,
        verbose: bool,
        **parameters: float | int,
    ) -> Iterator[GenerationResult]: ...


def token_budget(text: str) -> int:
    """The speech-latent ceiling a Block for `text` decodes to: the lane's
    runaway budget in latents."""
    return int(runaway_budget_seconds(text) * LATENTS_PER_SECOND)


def runaway_seconds(text: str) -> float:
    """The runaway budget in seconds of the latent ceiling VibeVoice decodes to."""
    return token_budget(text) / LATENTS_PER_SECOND


def generate(
    model: VibeVoiceModel,
    record: GenerationRecord,
    reference: VoiceReferenceAudio | None = None,
) -> Iterator[GenerationResult]:
    """Generate the record in its preset Voice, whose prompt cache mlx-audio
    reads from the promoted directory's `voices/`."""
    return model.generate(
        record.text.translate(_QUOTES),
        voice=record.voice_id,
        max_tokens=token_budget(record.text),
        stream=record.decode_mode == "streaming",
        streaming_interval=STREAMING_INTERVAL_SECONDS,
        verbose=False,
        **Parameters.model_validate(record.parameters).model_dump(exclude_none=True),
    )


def _load(model_dir: Path) -> VibeVoiceModel:
    from transformers import AutoTokenizer

    verify_bundled_tokenizer()
    model: VibeVoiceModel = load_without_hook(model_dir)
    model.tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_DIRECTORY)
    return model


class VibeVoiceArchitecture:
    """VibeVoice-Realtime's Architecture as the registry names it (ADR 0014)."""

    backend = "mlx-audio"
    conditioning = "preset"
    warmup_text = "Ready, Zyntrix."
    parameters = Parameters
    chunk_budget_candidates = None

    def expected_files(self, entry: CatalogEntry) -> frozenset[str]:
        # `generate` reads the chosen Voice's cache, so a Voice without its
        # file fails on its first Narration, not at load.
        return EXPECTED_FILES | {
            f"voices/{voice.id}.safetensors" for voice in entry.voices
        }

    def load(
        self, model_dir: Path, entry: CatalogEntry, *, references: VoiceReferences
    ) -> Synthesizer:
        return MlxSynthesizer(
            model_dir,
            generate=generate,
            runaway_seconds=runaway_seconds,
            model_factory=_load,
        )


ARCHITECTURE = VibeVoiceArchitecture()
