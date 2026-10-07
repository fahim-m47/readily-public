"""Supertonic's Architecture: what is unique to it is the four-graph flow a
draw runs through, the denoiser's `steps`, the trim to the predicted
duration, and the in-pause crackle it scrubs. Everything every Architecture
owes is `TestConformance`'s."""

import json
from pathlib import Path

import numpy as np
from conformance import Conformance
from fakes import FakeOnnxSession
from generation_fakes import record

from readily_engine.audio.artifacts import find_noise_bursts
from readily_engine.catalog import CatalogEntry, load_manifest
from readily_engine.generation import GenerationRecord
from readily_engine.loading.supertonic import (
    _GRAPH_NAMES,
    ARCHITECTURE,
    SUPERTONIC_SAMPLE_RATE,
    SupertonicSynthesizer,
)

# Answers for every graph but the vocoder: a one-second duration, an empty
# text embedding, and a denoiser whose every step adds one.
GRAPHS = {
    "duration_predictor": lambda _feed: [np.array([1.0], np.float32)],
    "text_encoder": lambda feed: [
        np.zeros((1, 3, feed["text_ids"].shape[-1]), np.float32)
    ],
    "vector_estimator": lambda feed: [feed["noisy_latent"] + 1],
}


def promote(model_dir: Path, voices: list[str]) -> None:
    """Write graph placeholders, the config and indexer `load` parses (the
    indexer maps ASCII codepoint `c` to `c + 1000`, but marks DEL unseen
    with -1), and a style per Voice."""
    onnx_dir = model_dir / "onnx"
    styles_dir = model_dir / "voice_styles"
    onnx_dir.mkdir(parents=True, exist_ok=True)
    styles_dir.mkdir(exist_ok=True)
    for graph in _GRAPH_NAMES:
        (onnx_dir / f"{graph}.onnx").write_bytes(b"placeholder")
    (onnx_dir / "tts.json").write_text(
        json.dumps(
            {
                "ae": {"sample_rate": 44_100, "base_chunk_size": 512},
                "ttl": {"chunk_compress_factor": 6, "latent_dim": 24},
            }
        ),
        encoding="utf-8",
    )
    (onnx_dir / "unicode_indexer.json").write_text(
        json.dumps([codepoint + 1_000 for codepoint in range(127)] + [-1]),
        encoding="utf-8",
    )
    style = {
        "style_ttl": {"dims": [1, 2, 3], "data": [[[1, 2, 3], [4, 5, 6]]]},
        "style_dp": {"dims": [1, 1, 2], "data": [[[7, 8]]]},
    }
    for voice in voices:
        (styles_dir / f"{voice}.json").write_text(json.dumps(style), encoding="utf-8")


class TestConformance(Conformance):
    model_id = "supertonic:66m"
    sample_rate = SUPERTONIC_SAMPLE_RATE
    runaway_budget = False
    # The predicted duration bounds the audio, so it outlasts every draw
    # the suite scripts.
    graphs = GRAPHS | {
        "duration_predictor": lambda _feed: [np.array([120.0], np.float32)]
    }

    def promote(self, model_dir: Path, entry: CatalogEntry) -> None:
        promote(model_dir, [voice.id for voice in entry.voices])


def loaded_model(tmp_path: Path, audio: np.ndarray | None = None, seconds: float = 1.0):
    """A synthesizer over `tmp_path` whose duration predictor answers
    `seconds` and whose vocoder speaks `audio` (three samples unless given),
    the sessions it opened by graph, and the paths it opened them from."""
    promote(tmp_path, ["M1"])
    if audio is None:
        audio = np.array([0.1, 0.2, -0.1], np.float32)
    answers = GRAPHS | {
        "duration_predictor": lambda _feed: [np.array([seconds], np.float32)],
        "vocoder": lambda _feed: [audio.reshape(1, -1)],
    }
    sessions: dict[str, FakeOnnxSession] = {}
    loaded_paths: list[Path] = []

    def session_factory(path: Path) -> FakeOnnxSession:
        loaded_paths.append(path)
        sessions[path.stem] = FakeOnnxSession(answers[path.stem])
        return sessions[path.stem]

    model = SupertonicSynthesizer(tmp_path, session_factory=session_factory)
    return model, sessions, loaded_paths, tmp_path


def test_a_steps_override_reaches_the_denoiser(tmp_path):
    model, sessions, _paths, _root = loaded_model(tmp_path)
    entry = load_manifest().find("supertonic:66m")
    model.generate(
        GenerationRecord.for_entry(entry.compose({"steps": 12}).entry, "M1", "Hello.")
    )
    calls = sessions["vector_estimator"].calls
    assert len(calls) == 12
    assert all(call["total_step"].item() == 12 for call in calls)


def test_generating_runs_the_pinned_four_graph_flow_for_english(tmp_path):
    model, sessions, loaded_paths, root = loaded_model(tmp_path)

    result = model.generate(record("Hello @ Readily", "M1", parameters={"steps": 8}))

    assert loaded_paths == [root / "onnx" / f"{name}.onnx" for name in _GRAPH_NAMES]
    assert result.sample_rate == SUPERTONIC_SAMPLE_RATE
    np.testing.assert_array_equal(
        result.pcm, np.array([0.1, 0.2, -0.1], dtype=np.float32)
    )

    duration_inputs = sessions["duration_predictor"].calls[0]
    normalized = "<en>Hello at Readily.</en>"
    assert duration_inputs["text_ids"].tolist() == [
        [ord(character) + 1_000 for character in normalized]
    ]
    assert duration_inputs["text_ids"].dtype == np.int64
    assert duration_inputs["text_mask"].shape == (1, 1, len(normalized))
    assert duration_inputs["style_dp"].shape == (1, 1, 2)

    vector_calls = sessions["vector_estimator"].calls
    assert len(vector_calls) == 8
    assert [call["current_step"].item() for call in vector_calls] == list(range(8))
    assert all(call["total_step"].item() == 8 for call in vector_calls)
    for step in range(1, len(vector_calls)):
        np.testing.assert_array_equal(
            vector_calls[step]["noisy_latent"],
            vector_calls[step - 1]["noisy_latent"] + 1,
        )
    vocoder_latent = sessions["vocoder"].calls[0]["latent"]
    np.testing.assert_array_equal(vocoder_latent, vector_calls[-1]["noisy_latent"] + 1)
    assert vocoder_latent.shape == (1, 144, 15)


def test_characters_the_model_never_saw_are_dropped(tmp_path):
    model, sessions, _loaded_paths, _root = loaded_model(tmp_path)

    model.generate(record("A\x7f\U00020000", "M1", parameters={"steps": 8}))

    duration_inputs = sessions["duration_predictor"].calls[0]
    kept = "<en>A.</en>"
    assert duration_inputs["text_ids"].tolist() == [
        [ord(character) + 1_000 for character in kept]
    ]
    assert duration_inputs["text_mask"].shape == (1, 1, len(kept))


def test_audio_past_the_predicted_duration_is_dropped(tmp_path):
    audio = np.full(SUPERTONIC_SAMPLE_RATE * 2, 0.1, np.float32)
    model, _sessions, _loaded_paths, _root = loaded_model(tmp_path, audio, 1.5)

    pcm = model.generate(record("Hello", "M1", parameters={"steps": 8})).pcm

    assert len(pcm) == int(SUPERTONIC_SAMPLE_RATE * 1.5)


def test_generation_uses_the_unscaled_predicted_duration(tmp_path):
    model, sessions, _loaded_paths, _root = loaded_model(tmp_path)

    model.generate(record("Hello", "M1", parameters={"steps": 8}))

    first_step = sessions["vector_estimator"].calls[0]
    assert first_step["noisy_latent"].shape == (1, 144, 15)


def test_generating_scrubs_supertonics_in_pause_crackle(tmp_path):
    # The fakes' tones are at 24kHz; Supertonic's audio is at 44.1kHz.
    rate = SUPERTONIC_SAMPLE_RATE
    time = np.arange(rate // 2, dtype=np.float32) / rate
    speech = (0.3 * np.sin(2 * np.pi * 400 * time)).astype(np.float32)
    crackle_time = np.arange(rate // 20, dtype=np.float32) / rate
    crackle = (0.1 * np.sin(2 * np.pi * 9_000 * crackle_time)).astype(np.float32)
    quiet = np.zeros(rate // 2, dtype=np.float32)
    raw = np.concatenate([speech, quiet, crackle, quiet, speech])
    assert len(find_noise_bursts(raw, rate)) == 1
    model, _sessions, _loaded_paths, _root = loaded_model(
        tmp_path, raw, len(raw) / rate
    )

    cleaned = model.generate(record("Hello", "M1", parameters={"steps": 8})).pcm

    assert find_noise_bursts(cleaned, rate) == ()
    np.testing.assert_array_equal(cleaned[: len(speech)], speech)


def test_the_architecture_expects_a_style_file_per_voice():
    entry = load_manifest().find("supertonic:66m")

    styles = {f"voice_styles/{voice.id}.json" for voice in entry.voices}
    assert styles <= ARCHITECTURE.expected_files(entry)
