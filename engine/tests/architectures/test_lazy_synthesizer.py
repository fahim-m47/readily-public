"""One lazy loader over every Architecture the registry names.

The promoted directory may not exist at Engine launch — it appears when the
store promotes a download — so construction must not touch it, and the first
Narration after a promotion must find the model without a restart.
"""

from pathlib import Path

import numpy as np
import pytest
from conftest import make_entry
from fakes import Store
from generation_fakes import record

from readily_engine.audio.encoding import write_float_wav
from readily_engine.catalog import load_manifest
from readily_engine.generation import DegenerateDraw, GeneratedAudio
from readily_engine.loading import LazySynthesizer, synthesizer_for
from readily_engine.loading.parameters import ParameterMismatch
from readily_engine.store import file_sha256

SAMPLE_RATE = 24_000

# The warm-up draw every lazy synthesizer here is built with. Tests recognise
# it by identity rather than by its words, which are the Engine's to choose.
WARMUP = record("Ready, Zyntrix.")

# The Voices every lazy synthesizer here offers: the shared record's own.
VOICES = frozenset({"narrator"})

SPEECH = (0.1 * np.sin(np.linspace(0, 200 * np.pi, 2400))).astype(np.float32)


class RecordingModel:
    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir
        self.warmed = 0

    def generate(self, generation):
        if generation == WARMUP:
            self.warmed += 1
        return GeneratedAudio(SPEECH, SAMPLE_RATE)


def recording_loader(loaded: list[RecordingModel]):
    def loader(model_dir: Path) -> RecordingModel:
        model = RecordingModel(model_dir)
        loaded.append(model)
        return model

    return loader


def test_construction_touches_nothing_and_first_use_loads_once(tmp_path):
    loaded: list[RecordingModel] = []
    lazy = LazySynthesizer(
        tmp_path / "not-yet",
        recording_loader(loaded),
        WARMUP,
        installed=lambda: False,
        voices=VOICES,
    )

    assert loaded == []

    lazy.generate(record("Hello"))
    lazy.generate(record("Hello again"))

    assert [model.model_dir for model in loaded] == [tmp_path / "not-yet"]


def test_prewarm_before_any_promotion_is_a_quiet_no_op(tmp_path):
    loaded: list[RecordingModel] = []
    promoted = tmp_path / "not-yet"
    lazy = LazySynthesizer(
        promoted,
        recording_loader(loaded),
        WARMUP,
        installed=promoted.is_dir,
        voices=VOICES,
    )

    assert lazy.prewarm() is False
    assert loaded == []

    # The model may be promoted later; the lazy path must still try then.
    (tmp_path / "not-yet").mkdir()
    assert lazy.prewarm() is True
    assert [model.warmed for model in loaded] == [1]


def test_a_prewarmed_model_is_reused_by_the_narrations_that_follow(tmp_path):
    loaded: list[RecordingModel] = []
    lazy = LazySynthesizer(
        tmp_path,
        recording_loader(loaded),
        WARMUP,
        installed=lambda: True,
        voices=VOICES,
    )

    assert lazy.prewarm() is True
    lazy.generate(record("Hello"))

    assert len(loaded) == 1
    assert loaded[0].warmed == 1


def test_a_load_failure_is_raised_to_the_caller_not_swallowed(tmp_path):
    def loader(model_dir: Path):
        raise FileNotFoundError(model_dir)

    lazy = LazySynthesizer(
        tmp_path, loader, WARMUP, installed=lambda: True, voices=VOICES
    )

    with pytest.raises(FileNotFoundError):
        lazy.generate(record("Hello"))


def with_reference(
    name: str, tag: str, architecture: str = "qwen3", *, sha256: str = "a" * 64
):
    return make_entry(
        name=name,
        tag=tag,
        architecture=architecture,
        voices=[
            {
                "id": "Chelsie",
                "name": "Chelsie",
                "language": "en-US",
                "reference": {
                    "clip": "qwen3-tts/0.6b/Chelsie.wav",
                    "text": "Hi.",
                    "sha256": sha256,
                },
            }
        ],
        default_voice="Chelsie",
    )


@pytest.mark.parametrize("model_id", ["qwen3-tts:0.6b", "future-model:small"])
@pytest.mark.parametrize("keep_referenced_voice", [False, True])
def test_a_required_reference_missing_from_any_voice_fails_at_engine_start(
    tmp_path, model_id, keep_referenced_voice
):
    name, tag = model_id.split(":")
    entry = with_reference(name, tag)
    voices = (
        [voice.model_dump() for voice in entry.voices] if keep_referenced_voice else []
    )
    voices.append({"id": "Ethan", "name": "Ethan", "language": "en-US"})
    entry = make_entry(
        name=name, tag=tag, architecture="qwen3", voices=voices, default_voice="Ethan"
    )

    with pytest.raises(ValueError):
        synthesizer_for(entry, Store(tmp_path / "store"), references_root=tmp_path)


def test_a_reference_the_bundle_lacks_fails_at_engine_start_not_on_a_narration(
    tmp_path,
):
    with pytest.raises(FileNotFoundError):
        synthesizer_for(
            with_reference("qwen3-tts", "0.6b"),
            Store(tmp_path / "store"),
            references_root=tmp_path,
        )


@pytest.mark.parametrize("decode_mode", ["streaming", "non-streaming"])
def test_a_reference_and_decode_mode_reach_the_generator(
    tmp_path, monkeypatch, decode_mode
):
    from readily_engine.loading import qwen3

    clip = tmp_path / "qwen3-tts/0.6b/Chelsie.wav"
    clip.parent.mkdir(parents=True)
    write_float_wav(clip, np.zeros(2400, dtype=np.float32), SAMPLE_RATE)
    wired = {}

    def fake_synthesizer(model_dir, *, generate, runaway_seconds, references):
        class RecordingLane:
            def generate(self, record):
                wired.update(record=record, references=references)
                return GeneratedAudio(np.zeros(2400, dtype=np.float32), SAMPLE_RATE)

        return RecordingLane()

    monkeypatch.setattr(qwen3, "MlxSynthesizer", fake_synthesizer)
    (tmp_path / "store/qwen3-tts/0.6b").mkdir(parents=True)

    entry = with_reference("qwen3-tts", "0.6b", sha256=file_sha256(clip)).model_copy(
        update={"decode_mode": decode_mode}
    )
    lazy = synthesizer_for(entry, Store(tmp_path / "store"), references_root=tmp_path)
    assert lazy.prewarm() is True

    references = wired["references"]
    digest = entry.voices[0].reference.sha256
    assert set(references) == {("Chelsie", digest)}
    assert references["Chelsie", digest].text == "Hi."
    assert len(references["Chelsie", digest].pcm) == 2400
    assert wired["record"].decode_mode == decode_mode
    assert wired["record"].reference_digest == digest


@pytest.mark.parametrize(
    ("name", "tag", "architecture"),
    [
        ("chatterbox", "turbo", "chatterbox_turbo"),
        ("kokoro", "82m", "kokoro"),
        ("kitten-tts", "15m", "kitten"),
    ],
)
def test_a_reference_on_a_voice_that_cannot_use_it_fails_at_engine_start(
    tmp_path, name, tag, architecture
):
    with pytest.raises(ValueError):
        synthesizer_for(
            with_reference(name, tag, architecture), Store(tmp_path / "store")
        )


def test_an_entry_naming_no_architecture_is_refused_rather_than_given_a_default(
    tmp_path,
):
    # An Architecture cannot be guessed from a name: Chatterbox and Chatterbox Turbo
    # are different mlx-audio classes under one family name, and an ONNX
    # entry handed Kokoro's graph inputs fails deep in onnxruntime, on a
    # Narration. The registry is the only map, so an id it lacks is refused
    # before any model is wired.
    entry = make_entry(name="chatterbox", tag="original", architecture="chatterbox")

    with pytest.raises(ValueError):
        synthesizer_for(entry, Store(tmp_path / "store"))


def test_an_entry_pinning_knobs_its_architecture_refuses_is_refused_at_boot(
    tmp_path,
):
    # Supertonic's denoiser has no step count to fall back on; an entry that
    # pins none fails here rather than on its first Narration.
    entry = make_entry(name="supertonic", tag="66m", architecture="supertonic")

    with pytest.raises(ParameterMismatch):
        synthesizer_for(entry, Store(tmp_path / "store"))


def test_every_committed_entry_resolves_to_an_architecture(tmp_path):
    # `EngineNarrator.__init__` builds one synthesizer per Catalog entry in a
    # single comprehension, so an entry the registry cannot place does not degrade
    # to that model being unavailable — it raises before any model is wired
    # and the Engine has no narrator at all. The blast radius of a Catalog
    # addition is therefore every model, which is why the check belongs on the
    # committed manifest rather than on the entry being added.
    for entry in load_manifest().models:
        assert isinstance(
            synthesizer_for(entry, Store(tmp_path / "store")), LazySynthesizer
        )


def refusing_loader(error: Exception):
    def loader(model_dir: Path):
        del model_dir

        class Refusing:
            def generate(self, record):
                del record
                raise error

        return Refusing()

    return loader


def test_a_gate_rejected_warm_up_draw_still_leaves_the_model_warm(tmp_path):
    promoted = tmp_path / "promoted"
    promoted.mkdir()
    lazy = LazySynthesizer(
        promoted,
        refusing_loader(DegenerateDraw("every synthesis draw was degenerate")),
        WARMUP,
        installed=lambda: True,
        voices=VOICES,
    )

    assert lazy.prewarm() is True


def test_a_warm_up_that_fails_for_any_other_reason_still_raises(tmp_path):
    promoted = tmp_path / "promoted"
    promoted.mkdir()
    lazy = LazySynthesizer(
        promoted,
        refusing_loader(RuntimeError("the graph failed")),
        WARMUP,
        installed=lambda: True,
        voices=VOICES,
    )

    with pytest.raises(RuntimeError):
        lazy.prewarm()


def test_a_voice_the_entry_does_not_offer_is_refused_before_any_load(tmp_path):
    loaded: list[RecordingModel] = []
    lazy = LazySynthesizer(
        tmp_path,
        recording_loader(loaded),
        WARMUP,
        installed=lambda: True,
        voices=VOICES,
    )

    with pytest.raises(ValueError):
        lazy.generate(record(voice="nobody"))
    assert loaded == []
