"""Kokoro's Architecture: what is unique to it is the dialect a Voice is
phonemized in and the duration vector its timed export exposes. Everything
every Architecture owes is `TestConformance`'s."""

# IPA test fixtures are intentionally confusable with Latin letters.
# ruff: noqa: RUF001
from pathlib import Path

import numpy as np
from conformance import Conformance
from fakes import FakeOnnxSession
from generation_fakes import record

from readily_engine.catalog import CatalogEntry
from readily_engine.loading.styletts2 import (
    SAMPLE_RATE,
    StyleTTS2Synthesizer,
    misaki_g2p,
)
from readily_engine.loading.styletts2.kokoro import MODEL_FILE, SPEC, VOICES_FILE


def promote(model_dir: Path, voices: list[str]) -> None:
    """Write a graph placeholder and the voice archive: one style row per
    phoneme count, per Voice."""
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / MODEL_FILE).write_bytes(b"placeholder")
    with (model_dir / VOICES_FILE).open("wb") as output:
        np.savez(
            output, **{voice: np.ones((510, 1, 256), np.float32) for voice in voices}
        )


class TestConformance(Conformance):
    model_id = "kokoro:82m"
    sample_rate = SAMPLE_RATE
    runaway_budget = False

    def promote(self, model_dir: Path, entry: CatalogEntry) -> None:
        promote(model_dir, [voice.id for voice in entry.voices])


def test_a_voice_is_phonemized_in_the_dialect_its_id_names(tmp_path):
    promote(tmp_path, ["af_heart", "bf_emma"])
    session = FakeOnnxSession(lambda _feed: [np.zeros(4800, np.float32)])
    built: list[str] = []

    def g2p_factory(dialect):
        built.append(dialect)
        phonemes = {"american": "wˈɔTəɹ", "british": "wˈɔːtə"}[dialect]
        return lambda text: (phonemes, [text])

    synthesizer = StyleTTS2Synthesizer(
        tmp_path,
        SPEC,
        session_factory=lambda _path: session,
        g2p_factory=g2p_factory,
    )
    assert built == ["american"]

    def spoken_ids(voice: str) -> list[int]:
        synthesizer.generate(record("water", voice))
        return session.inputs["tokens"].tolist()[0]

    assert spoken_ids("bf_emma") == [0, 65, 156, 76, 158, 62, 83, 0]
    assert spoken_ids("af_heart") == [0, 65, 156, 76, 36, 83, 123, 0]
    assert spoken_ids("bf_emma") == [0, 65, 156, 76, 158, 62, 83, 0]
    assert built == ["american", "british"]
    assert session.inputs["tokens"].dtype == np.int64
    assert session.inputs["style"].shape == (1, 256)
    assert session.inputs["speed"].tolist() == [1.0]


def test_duration_output_reaches_the_spoken_channel(tmp_path):
    promote(tmp_path, ["af_heart"])
    session = FakeOnnxSession(
        lambda _feed: [np.zeros(4800, np.float32), np.array([2, 1, 1, 2, 2])]
    )
    synthesizer = StyleTTS2Synthesizer(
        tmp_path,
        SPEC,
        session_factory=lambda _path: session,
        g2p_factory=misaki_g2p,
    )

    result = synthesizer.generate(record("Hi", "af_heart"))

    assert [
        (t.start_char, t.end_char, t.start_sec, t.end_sec, t.provenance)
        for t in result.timings
    ] == [(0, 2, 0.05, 0.15, "spoken")]
