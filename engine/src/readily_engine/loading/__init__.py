"""Model loading: one Architecture package per Voice Model, the registry
that names them, and the lanes they share (ADR 0014).

The ONLY package that may call model-loading APIs, and it loads exclusively
from paths the store has promoted or the hash-verified bundled pronunciation
data — never from staging or arbitrary paths (threat model B2; Semgrep
no-load-without-verification rule). Allowed
weight formats: safetensors and ONNX; sidecar assets as `.npz`/`.npy` with
`allow_pickle=False` and plain JSON. pickle-family loaders are banned
Engine-wide.

MLX imports stay confined here so everything else tests on ubuntu; MLX code
paths belong to the macOS smoke job (which grows an Engine lane when MLX
code lands), never to unit tests.
"""

import gc
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from readily_engine.audio.artifacts import SILENCE_RMS, frame_rms
from readily_engine.catalog import REFERENCE_ROOT, CatalogEntry, PinnedArtifact
from readily_engine.generation import (
    DegenerateDraw,
    GeneratedAudio,
    GenerationRecord,
    Synthesizer,
)
from readily_engine.loading.parameters import check_parameters

logger = logging.getLogger(__name__)


class PromotedDirs(Protocol):
    def promoted_dir(self, entry: PinnedArtifact) -> Path: ...
    def installed(self, entry: PinnedArtifact) -> bool: ...


class LazySynthesizer:
    """Load a Voice Model from its promoted directory on first use, not at
    Engine launch: the directory may not exist yet — it appears when the
    store promotes a download — and once it does, the next Narration finds
    it without a restart. Called only on the generation worker's one thread,
    so `_loaded` needs no lock.

    `installed` is the store's word on whether the directory may be loaded
    at boot: a directory can exist and still be one the store has marked
    broken, so existing is never taken as installed.

    What every Architecture owes a Narration is enforced here rather than in
    each Architecture: a record naming a Voice outside `voices` is refused
    before any load, and a draw with no frame above the silence floor is a
    `DegenerateDraw`, so the worker redraws it instead of storing silence.

    `unload` drops the model, then calls `release` to hand its Backend's
    cached memory back; the next use loads it again. It releases even with
    no model loaded, because a load cut short can leave memory behind.
    """

    def __init__(
        self,
        model_dir: Path,
        loader: Callable[[Path], Synthesizer],
        warmup: GenerationRecord,
        installed: Callable[[], bool],
        *,
        voices: frozenset[str],
        release: Callable[[], None] = lambda: None,
    ) -> None:
        self.warmup = warmup
        self._release = release
        self._voices = voices
        self._model_dir = model_dir
        self._loader = loader
        self._installed = installed
        self._loaded: Synthesizer | None = None

    def prewarm(self) -> bool:
        """Load and warm the model at boot when it is already promoted, so
        user-visible TTFA lands at warm levels (ADR 0002). Not promoted yet
        is the normal first-launch state: report False and leave the lazy
        first-use load in place."""
        if self._loaded is None:
            if not self._installed():
                return False
            self._loaded = self._loader(self._model_dir)
        try:
            self.generate(self.warmup)
        except DegenerateDraw:
            # Warm-up runs the graph and throws its audio away, and its seed
            # is fixed, so a rejected draw would be rejected on every boot.
            logger.warning("The warm-up draw was rejected; the model is warm")
        return True

    def unload(self) -> None:
        self._loaded = None
        gc.collect()
        self._release()

    def generate(self, record: GenerationRecord) -> GeneratedAudio:
        if record.voice_id not in self._voices:
            raise ValueError(f"{record.model_id} offers no voice {record.voice_id!r}")
        if self._loaded is None:
            self._loaded = self._loader(self._model_dir)
        audio = self._loaded.generate(record)
        _frames, rms = frame_rms(audio.pcm, audio.sample_rate // 100)
        if not (rms > SILENCE_RMS).any():
            raise DegenerateDraw("synthesis draw was silent")
        return audio


def synthesizer_for(
    entry: CatalogEntry, store: PromotedDirs, *, references_root: Path = REFERENCE_ROOT
) -> LazySynthesizer:
    """The Catalog's `architecture` field, finally read: resolve the entry's
    Architecture through the registry and wrap its `load` behind the store's
    promoted directory. The entry's Voices must carry a Voice Reference
    exactly when the Architecture conditions on one, and the clips are verified and
    decoded here, at Engine start, so a bundle missing one fails before any
    Narration does (threat model B2). Its knobs are checked against the
    Architecture's schema here too, for the same reason."""
    # Imported here, not at module top: the registry imports every Architecture, and
    # an Architecture imports this module's `PromotedDirs`.
    from readily_engine.loading.mlx_lane import release_mlx_cache
    from readily_engine.loading.references import voice_references
    from readily_engine.loading.registry import architecture_named

    architecture = architecture_named(entry.architecture)
    check_parameters(architecture.parameters, entry)
    referenced = [voice.id for voice in entry.voices if voice.reference is not None]
    unreferenced = [voice.id for voice in entry.voices if voice.reference is None]
    if architecture.conditioning == "reference" and unreferenced:
        raise ValueError(
            f"{entry.id} requires a Voice Reference, yet "
            f"{', '.join(unreferenced)} lack one"
        )
    if architecture.conditioning == "preset" and referenced:
        raise ValueError(
            f"{entry.id} cannot condition on a Voice Reference, yet "
            f"{', '.join(referenced)} name one"
        )
    references = voice_references(entry, references_root)

    def loader(path: Path) -> Synthesizer:
        return architecture.load(path, entry, references=references)

    return LazySynthesizer(
        store.promoted_dir(entry),
        loader,
        GenerationRecord.for_entry(
            entry, entry.default_voice, architecture.warmup_text
        ),
        installed=lambda: store.installed(entry),
        voices=frozenset(voice.id for voice in entry.voices),
        # An onnxruntime session frees its memory as it is collected; MLX
        # keeps freed buffers cached until told otherwise.
        release=release_mlx_cache
        if architecture.backend == "mlx-audio"
        else lambda: None,
    )
