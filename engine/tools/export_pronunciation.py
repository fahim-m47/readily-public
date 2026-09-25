"""Convert the hash-pinned NRC checkpoint and verify the Engine's decoder.

Dev only. Run with the separate tools project's `export` extra, never in the
Engine environment. The checkpoint is a local input; this script downloads
nothing and refuses different bytes before invoking the pickle loader.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import torch
from deep_phonemizer.model.model import ForwardTransformer
from deep_phonemizer.model.utils import get_dedup_tokens

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from readily_engine.loading.pronunciation import (
    PHONEME_SYMBOLS,
    TEXT_SYMBOLS,
    decode_logits,
    word_ids,
)

CHECKPOINT_SHA256 = "ee18c945608c0f2a52a4dc3edb21546a161911bde86be19052ff9895cec2483f"
WORDS = (
    "Marisol",
    "Okwuosa",
    "Xiomara",
    "Oyelaran",
    "Zyntrix",
    "snozzberry",
    "Nkemdirim",
    "Okonkwo",
    "Bassey",
    "Ryanair",
    "Schrödinger",
    "Siobhan",
    "Joaquin",
    "Nguyen",
    "Zeltronix",
    "Tallahassee",
    "Reykjavik",
    "hello",
    "quokka",
    "gizmo",
    "a",
    "x" * 84,
)


def strip_export_paths(graph: Path) -> None:
    """Drop the per-node source traces, which name the exporting machine.

    The dynamo exporter records the absolute path of every Python frame it
    traced through. That says nothing about the graph and everything about
    whoever ran the export, so it never ships.

    The key below is the one torch uses today. The check afterwards is what
    actually holds the promise: if torch renames the key, moves the traces
    into local functions, or finds some new way to write the export machine
    into the graph, the export fails here instead of shipping quietly.
    """
    model = onnx.load(graph)
    for node in model.graph.node:
        kept = [
            prop
            for prop in node.metadata_props
            if prop.key != "pkg.torch.onnx.stack_trace"
        ]
        del node.metadata_props[:]
        node.metadata_props.extend(kept)
    onnx.save(model, graph)
    leaked = re.search(rb"/(?:Users|home|private)/", graph.read_bytes())
    if leaked is not None:
        raise ValueError(f"{graph.name} still carries an absolute path")


class ExportGraph(torch.nn.Module):
    """Expose the forward Transformer's text tensor as the ONNX input."""

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, text):
        return self.model.forward({"text": text})


def export(checkpoint_path: Path, output: Path, *, check_only: bool) -> None:
    """Export safe runtime files, then compare them with the pinned original."""
    actual = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    if actual != CHECKPOINT_SHA256:
        raise ValueError(f"checkpoint SHA-256 mismatch: {actual}")
    # Dev-only exception: the exact NRC pickle is verified above. Never ship
    # this loader or its dependencies in the Engine (B2).
    checkpoint = torch.load(  # nosemgrep: engine-no-pickle-family-loaders
        checkpoint_path, map_location="cpu", weights_only=False
    )
    pre = checkpoint["preprocessor"]
    assert tuple(pre.text_tokenizer.token_to_idx) == TEXT_SYMBOLS
    assert tuple(pre.phoneme_tokenizer.token_to_idx) == PHONEME_SYMBOLS
    assert pre.text_tokenizer.char_repeats == 3
    assert len(checkpoint["phoneme_dict"]["en_us"]) == 123892
    model = ForwardTransformer.from_config(checkpoint["config"])
    model.load_state_dict(checkpoint["model"])
    model.eval()
    output.mkdir(parents=True, exist_ok=True)
    graph = output / "g2p.onnx"
    if not check_only:
        example = torch.tensor([pre.text_tokenizer("Nkemdirim", "en_us")])
        program = torch.onnx.export(
            ExportGraph(model).eval(),
            (example,),
            dynamo=True,
            dynamic_shapes={"text": {1: torch.export.Dim("chars", min=3, max=256)}},
            input_names=["text"],
            output_names=["logits"],
            opset_version=18,
        )
        program.save(graph, external_data=False)
        strip_export_paths(graph)
        (output / "lexicon.json").write_text(
            json.dumps(
                checkpoint["phoneme_dict"]["en_us"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
    lexicon = json.loads((output / "lexicon.json").read_text(encoding="utf-8"))
    assert lexicon == checkpoint["phoneme_dict"]["en_us"]
    ort.disable_telemetry_events()
    session = ort.InferenceSession(str(graph), providers=["CPUExecutionProvider"])
    with torch.inference_mode():
        for word in WORDS:
            reference_ids = pre.text_tokenizer(word, "en_us")
            assert word_ids(word) == reference_ids, word
            logits = model.forward({"text": torch.tensor([reference_ids])})
            tokens, _ = get_dedup_tokens(logits)
            expected = "".join(
                pre.phoneme_tokenizer.decode(
                    tokens[0].tolist(),
                    remove_special_tokens=True,
                )
            )
            actual = decode_logits(
                session.run(
                    None,
                    {
                        "text": np.array([word_ids(word)], dtype=np.int64),
                    },
                )[0]
            )
            assert actual == expected, (word, expected, actual)
            print(f"{word}: {actual}")
    print(f"Parity: {len(WORDS)}/{len(WORDS)}; lexicon: {len(lexicon)} entries")
    for path in (graph, output / "lexicon.json"):
        print(
            json.dumps(
                {
                    "path": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    export(args.checkpoint, args.output, check_only=args.check_only)
