"""The fixed Supertonic 2 ONNX loader (threat model B2).

Production hands this loader only a store-promoted directory. It has no
download path and loads the four graphs and their JSON sidecars from there.

The inference and normalization flow is derived from ``py/helper.py`` in
Supertone Inc.'s https://github.com/supertone-inc/supertonic repository at
commit 7e2804f96016a7028cb1ed627353c61c1e9dd281.

MIT License

Copyright (c) 2025 Supertone Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

# Unicode punctuation is intentional: this normalization table mirrors the
# pinned export's tokenizer contract.
# ruff: noqa: RUF001

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field

from readily_engine.audio.artifacts import scrub_noise_bursts
from readily_engine.catalog import CatalogEntry
from readily_engine.generation import (
    GeneratedAudio,
    GenerationRecord,
    Synthesizer,
)
from readily_engine.loading.onnx import Session, onnx_session
from readily_engine.loading.references import VoiceReferences

SUPERTONIC_SAMPLE_RATE = 44_100

_GRAPH_NAMES = (
    "duration_predictor",
    "text_encoder",
    "vector_estimator",
    "vocoder",
)
_EMOJI = re.compile(
    "[\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\u2600-\u26ff"
    "\u2700-\u27bf"
    "\U0001f1e6-\U0001f1ff]+"
)
_ENDING_PUNCTUATION = re.compile(r"[.!?;:,'\"')\]}…。」』】〉》›»]$")
_REPLACEMENTS = {
    "–": "-",
    "‑": "-",
    "—": "-",
    "_": " ",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "´": "'",
    "`": "'",
    "[": " ",
    "]": " ",
    "|": " ",
    "/": " ",
    "#": " ",
    "→": " ",
    "←": " ",
    "@": " at ",
    "e.g.,": "for example, ",
    "i.e.,": "that is, ",
}


@dataclass(frozen=True, slots=True)
class _Style:
    ttl: npt.NDArray[np.float32]
    dp: npt.NDArray[np.float32]


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_english(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    normalized = _EMOJI.sub("", normalized)
    for old, new in _REPLACEMENTS.items():
        normalized = normalized.replace(old, new)
    normalized = re.sub(r"[♥☆♡©\\]", "", normalized)
    normalized = re.sub(r" ([,.!?;:])", r"\1", normalized)
    normalized = re.sub(r" '\b", "'", normalized)
    for duplicate in ('""', "''", "``"):
        while duplicate in normalized:
            normalized = normalized.replace(duplicate, duplicate[0])
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not _ENDING_PUNCTUATION.search(normalized):
        normalized += "."
    return f"<en>{normalized}</en>"


def _length_mask(lengths: npt.NDArray[np.int64]) -> npt.NDArray[np.float32]:
    positions = np.arange(int(lengths.max()))
    mask = positions < np.expand_dims(lengths, axis=1)
    return mask.astype(np.float32).reshape(-1, 1, positions.size)


def _load_style(path: Path) -> _Style:
    raw = _read_json(path)

    def tensor(name: str) -> npt.NDArray[np.float32]:
        value = raw[name]
        return np.asarray(value["data"], dtype=np.float32).reshape(value["dims"])

    return _Style(ttl=tensor("style_ttl"), dp=tensor("style_dp"))


class SupertonicSynthesizer:
    """Load Supertonic 2's pinned four-graph export and narrate English."""

    def __init__(
        self,
        model_dir: Path,
        *,
        session_factory: Callable[[Path], Session] = onnx_session,
    ) -> None:
        onnx_dir = model_dir / "onnx"
        config_path = onnx_dir / "tts.json"
        indexer_path = onnx_dir / "unicode_indexer.json"
        graph_paths = [onnx_dir / f"{name}.onnx" for name in _GRAPH_NAMES]
        style_paths = sorted((model_dir / "voice_styles").glob("*.json"))
        required = [config_path, indexer_path, *graph_paths]
        if not all(path.is_file() for path in required) or not style_paths:
            raise FileNotFoundError(f"no promoted Supertonic model at {model_dir}")

        config = _read_json(config_path)
        self._sample_rate = int(config["ae"]["sample_rate"])
        if self._sample_rate != SUPERTONIC_SAMPLE_RATE:
            raise ValueError(f"unsupported Supertonic sample rate: {self._sample_rate}")
        self._base_chunk_size = int(config["ae"]["base_chunk_size"])
        self._compress = int(config["ttl"]["chunk_compress_factor"])
        self._latent_dim = int(config["ttl"]["latent_dim"])
        self._unicode_indexer = np.asarray(_read_json(indexer_path), dtype=np.int64)
        self._styles = {path.stem: _load_style(path) for path in style_paths}
        (
            self._duration_predictor,
            self._text_encoder,
            self._vector_estimator,
            self._vocoder,
        ) = (session_factory(path) for path in graph_paths)

    def _text_inputs(
        self, text: str
    ) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float32]]:
        normalized = _normalize_english(text)
        unicode_values = np.fromiter(map(ord, normalized), dtype=np.uint32)
        supported = unicode_values < len(self._unicode_indexer)
        text_ids = np.full(unicode_values.shape, -1, dtype=np.int64)
        text_ids[supported] = self._unicode_indexer[unicode_values[supported]]
        text_ids = text_ids.reshape(1, -1)
        lengths = np.asarray([text_ids.shape[1]], dtype=np.int64)
        return text_ids, _length_mask(lengths)

    def _noisy_latent(
        self, duration: npt.NDArray[np.float32], rng: np.random.Generator
    ) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
        wav_lengths = (duration * self._sample_rate).astype(np.int64)
        chunk_size = self._base_chunk_size * self._compress
        latent_lengths = (wav_lengths + chunk_size - 1) // chunk_size
        latent_frames = int(latent_lengths.max())
        noise = rng.standard_normal(
            (len(duration), self._latent_dim * self._compress, latent_frames)
        ).astype(np.float32)
        mask = _length_mask(latent_lengths)
        return noise * mask, mask

    def generate(self, record: GenerationRecord) -> GeneratedAudio:
        text, voice = record.text, record.voice_id
        style = self._styles.get(voice)
        if style is None:
            raise ValueError(f"unknown Supertonic voice: {voice}")
        text_ids, text_mask = self._text_inputs(text)
        duration = np.asarray(
            self._duration_predictor.run(
                None,
                {
                    "text_ids": text_ids,
                    "style_dp": style.dp,
                    "text_mask": text_mask,
                },
            )[0],
            dtype=np.float32,
        )
        text_embedding = self._text_encoder.run(
            None,
            {
                "text_ids": text_ids,
                "style_ttl": style.ttl,
                "text_mask": text_mask,
            },
        )[0]
        latent, latent_mask = self._noisy_latent(
            duration, np.random.default_rng(record.rng_seed)
        )
        steps = Parameters.model_validate(record.parameters).steps
        total_step = np.asarray([steps], dtype=np.float32)
        for step in range(steps):
            latent = self._vector_estimator.run(
                None,
                {
                    "noisy_latent": latent,
                    "text_emb": text_embedding,
                    "style_ttl": style.ttl,
                    "text_mask": text_mask,
                    "latent_mask": latent_mask,
                    "current_step": np.asarray([step], dtype=np.float32),
                    "total_step": total_step,
                },
            )[0]
        pcm = np.asarray(
            self._vocoder.run(None, {"latent": latent})[0], dtype=np.float32
        ).ravel()
        cleaned, _bursts = scrub_noise_bursts(pcm, self._sample_rate)
        return GeneratedAudio(cleaned, self._sample_rate)


# The graphs and sidecars every Supertonic entry pins; the per-Voice style
# files under `voice_styles/` are named by the entry's Voices, not here.
EXPECTED_FILES = frozenset(
    {"onnx/tts.json", "onnx/unicode_indexer.json"}
    | {f"onnx/{name}.onnx" for name in _GRAPH_NAMES}
)


# The denoiser has no step count of its own to fall back on, so every
# entry must say how many steps it takes.
class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    steps: int = Field(gt=0, strict=True)


class SupertonicArchitecture:
    """Supertonic 2's Architecture as the registry names it (ADR 0014)."""

    expected_files = EXPECTED_FILES
    conditioning = "preset"
    warmup_text = "Ready, Zyntrix."
    parameters = Parameters
    chunk_budget_candidates = None

    def load(
        self, model_dir: Path, entry: CatalogEntry, *, references: VoiceReferences
    ) -> Synthesizer:
        return SupertonicSynthesizer(model_dir)


ARCHITECTURE = SupertonicArchitecture()
