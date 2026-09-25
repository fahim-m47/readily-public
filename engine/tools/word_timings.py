"""Check spoken coordinates on both pinned exports over the 20-Block passage.

Run with `uv run --project engine python engine/tools/word_timings.py MODEL_ROOT`.
Uses already installed, hash-verified models; downloads nothing.
"""

import argparse
from pathlib import Path

from readily_engine.catalog import load_manifest
from readily_engine.curation.continuity import BLOCKS
from readily_engine.generation import GenerationRecord
from readily_engine.loading.styletts2 import StyleTTS2Synthesizer, kitten, kokoro
from readily_engine.narration.assembly import trim_range
from readily_engine.store import file_sha256
from readily_engine.timings import place_timings, word_spans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_root", type=Path)
    args = parser.parse_args()
    manifest = load_manifest()
    for model_id, spec in (
        ("kokoro:82m", kokoro.SPEC),
        ("kitten-tts:15m", kitten.SPEC),
    ):
        entry = manifest.resolve(model_id)
        root = args.model_root / entry.name / entry.tag
        for file in entry.files:
            assert file_sha256(root / file.path) == file.sha256
        synth = StyleTTS2Synthesizer(root, spec)
        total = 0
        for ordinal, text in enumerate(BLOCKS):
            result = synth.generate(
                GenerationRecord.for_entry(entry, entry.default_voice, text)
            )
            assert [(t.start_char, t.end_char) for t in result.timings] == word_spans(
                text
            ), (model_id, ordinal, "incomplete raw timings")
            margin = entry.tunables.trim_margin_ms
            start, end = trim_range(
                result.pcm,
                result.sample_rate,
                lead_margin_ms=margin,
                tail_margin_ms=margin,
            )
            words = place_timings(
                result.timings,
                text=text,
                source_start=0,
                trim_start_sec=start / result.sample_rate,
                duration_sec=(end - start) / result.sample_rate,
                timeline_start_sec=0,
            )
            assert all(w.provenance == "spoken" for w in words), (model_id, ordinal)
            total += len(words)
            print(f"{model_id} Block {ordinal}: {len(words)} spoken words", flush=True)
        print(
            f"{model_id}: {len(BLOCKS)} Blocks, {total} spoken words, no estimates",
            flush=True,
        )


if __name__ == "__main__":
    main()
