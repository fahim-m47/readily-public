"""The StyleTTS2 family synthesizer reads everything that differs between
its exports off the `Spec` it is handed, so a third export is a spec file
and never a synthesizer of its own."""

# IPA test fixtures are intentionally confusable with Latin letters.
# ruff: noqa: RUF001
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeOnnxSession
from generation_fakes import record

from readily_engine.catalog import load_manifest
from readily_engine.loading.styletts2 import (
    SAMPLE_RATE,
    Spec,
    StyleTTS2Architecture,
    StyleTTS2Synthesizer,
)

VOCAB = {" ": 1, "h": 2, "ˈ": 3, "a": 4, "ɪ": 5, "H": 6}


def spec(**overrides) -> Spec:
    fields = {
        "name": "Synthetic",
        "model_file": "graph.onnx",
        "voices_file": "voices.npz",
        "tokens_input": "ids",
        "vocab": VOCAB,
        "max_phonemes": 8,
        "style_by_length": False,
        "dialect": lambda _voice: "american",
        "respell": str,
        "repair": lambda pcm: pcm,
    }
    return Spec(**{**fields, **overrides})


def promote(model_dir: Path, style: np.ndarray) -> Path:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "graph.onnx").write_bytes(b"placeholder")
    with (model_dir / "voices.npz").open("wb") as output:
        np.savez(output, alice=style)
    return model_dir


def speaking(pcm: np.ndarray | None = None) -> FakeOnnxSession:
    if pcm is None:
        pcm = np.array([0.1, 0.2, -0.1], dtype=np.float32)
    return FakeOnnxSession(lambda _feed: [pcm])


def synthesizer(
    root: Path, spec: Spec, session: FakeOnnxSession, phonemes: str = "hˈaɪ"
) -> StyleTTS2Synthesizer:
    return StyleTTS2Synthesizer(
        root,
        spec,
        session_factory=lambda _path: session,
        g2p_factory=lambda _dialect: lambda text: (phonemes, [text]),
    )


def test_the_graph_is_fed_under_the_specs_own_input_names(tmp_path):
    session = speaking()
    root = promote(tmp_path, np.ones((1, 4), np.float32))

    audio = synthesizer(root, spec(), session).generate(record("Hi", "alice"))

    assert session.inputs["ids"].tolist() == [[0, 2, 3, 4, 5, 0]]
    assert session.inputs["ids"].dtype == np.int64
    assert session.inputs["style"].shape == (1, 4)
    assert session.inputs["speed"].tolist() == [1.0]
    assert audio.sample_rate == SAMPLE_RATE


def test_a_style_archive_with_a_row_per_phoneme_count_is_read_by_count(tmp_path):
    rows = np.arange(8, dtype=np.float32).reshape(8, 1, 1) * np.ones((8, 1, 4))
    root = promote(tmp_path, rows.astype(np.float32))
    session = speaking()

    synthesizer(root, spec(style_by_length=True), session).generate(
        record("Hi", "alice")
    )

    # Four phonemes, so row 3.
    assert session.inputs["style"].tolist() == [[3.0, 3.0, 3.0, 3.0]]


def test_phonemes_are_respelled_before_they_are_looked_up(tmp_path):
    session = speaking()
    root = promote(tmp_path, np.ones((1, 4), np.float32))
    custom = spec(respell=lambda phonemes: phonemes.replace("h", "H"))

    synthesizer(root, custom, session).generate(record("Hi", "alice"))

    assert session.inputs["ids"].tolist() == [[0, 6, 3, 4, 5, 0]]


def test_a_dialects_g2p_is_built_once_and_only_when_a_voice_asks(tmp_path):
    root = promote(tmp_path, np.ones((1, 4), np.float32))
    built: list[str] = []

    def g2p_factory(dialect):
        built.append(dialect)
        return lambda text: ("hˈaɪ", [text])

    custom = spec(dialect=lambda voice: "british" if voice == "alice" else "american")
    instance = StyleTTS2Synthesizer(
        root, custom, session_factory=lambda _path: speaking(), g2p_factory=g2p_factory
    )
    assert built == ["american"]

    instance.generate(record("Hi", "alice"))
    instance.generate(record("Hi", "alice"))

    assert built == ["american", "british"]


def test_the_specs_repair_is_the_last_thing_the_draw_goes_through(tmp_path):
    root = promote(tmp_path, np.ones((1, 4), np.float32))
    drawn = np.array([0.1, 0.2, -0.1], dtype=np.float32)
    custom = spec(repair=lambda pcm: pcm * 2)

    audio = synthesizer(root, custom, speaking(drawn)).generate(record("Hi", "alice"))

    np.testing.assert_array_equal(audio.pcm, drawn * 2)


def test_a_block_past_the_specs_phoneme_limit_is_refused_unspoken(tmp_path):
    root = promote(tmp_path, np.ones((1, 4), np.float32))
    session = speaking()

    with pytest.raises(ValueError, match="phoneme limit"):
        synthesizer(root, spec(), session, "a" * 9).generate(record("Hi", "alice"))
    assert not session.calls


def test_a_voice_the_archive_lacks_is_refused_unspoken(tmp_path):
    root = promote(tmp_path, np.ones((1, 4), np.float32))
    session = speaking()

    with pytest.raises(ValueError, match="voice"):
        synthesizer(root, spec(), session).generate(record("Hi", "bob"))
    assert not session.calls


def test_the_architecture_expects_the_specs_two_files():
    architecture = StyleTTS2Architecture(spec())
    entry = load_manifest().find("kokoro:82m")

    assert architecture.expected_files(entry) == frozenset({"graph.onnx", "voices.npz"})
