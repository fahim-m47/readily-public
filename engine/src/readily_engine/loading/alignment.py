"""Load the Catalog's shared aligner only from its verified model directory."""

import json

import numpy as np

from readily_engine.alignment import PCM, CTCAligner
from readily_engine.catalog import SupportModel
from readily_engine.loading import PromotedDirs
from readily_engine.loading.onnx import onnx_session
from readily_engine.timings import Timing


class ForcedAligner:
    """Lazily keep one ONNX session on the serial generation thread."""

    def __init__(self, entry: SupportModel, store: PromotedDirs) -> None:
        self._entry = entry
        self._store = store
        self._aligner: CTCAligner | None = None

    def align(self, text: str, pcm: PCM, sample_rate: int) -> tuple[Timing, ...]:
        if self._aligner is None:
            self._aligner = self._load()
        return self._aligner.align(text, pcm, sample_rate)

    def _load(self) -> CTCAligner:
        root = self._store.promoted_dir(self._entry)
        vocabulary = json.loads((root / "vocab.json").read_text())
        if (
            not isinstance(vocabulary, dict)
            or len(vocabulary) != 32
            or set(vocabulary.values()) != set(range(32))
            or vocabulary.get("<pad>") != 0
            or vocabulary.get("|") != 4
        ):
            raise ValueError("unsupported wav2vec2 vocabulary")
        labels = tuple(sorted(vocabulary, key=vocabulary.__getitem__))
        config = json.loads((root / "preprocessor_config.json").read_text())
        if (
            config.get("sampling_rate") != 16000
            or config.get("do_normalize") is not True
            or config.get("return_attention_mask") is not False
            or config.get("feature_size") != 1
        ):
            raise ValueError("unsupported wav2vec2 preprocessing")
        session = onnx_session(root / "onnx/model.onnx")

        def infer(pcm: PCM) -> PCM:
            values = (pcm - pcm.mean()) / np.sqrt(pcm.var() + 1e-7)
            logits = session.run(
                None, {"input_values": values[None].astype(np.float32)}
            )[0]
            if logits.ndim != 3 or logits.shape[0] != 1:
                raise ValueError("unsupported wav2vec2 output shape")
            logits = logits[0] - logits[0].max(axis=-1, keepdims=True)
            return logits - np.log(np.exp(logits).sum(axis=-1, keepdims=True))

        return CTCAligner(infer, labels)
