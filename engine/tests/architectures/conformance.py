"""What "supported" means for an Architecture (ADR 0014 §4): the one suite
every registered Architecture runs, across the seam the Engine reaches it
through — `synthesizer_for` over its committed Catalog entry, its own `load`,
and the Backend faked underneath it (`fakes.FakeOnnxRuntime`, `fakes.FakeMlx`).

An Architecture joins by subclassing `Conformance` as `TestConformance` in
its own `test_<architecture>.py`, naming its committed entry and declared
sample rate, and writing in `promote` whatever sidecar files its `load`
parses; every other expected file is written as placeholder bytes. Nothing
here names an Architecture, so detaching one changes nothing in this file.
`test_registry.py` refuses a registered Architecture without a subclass."""

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
from annotated_types import Ge, Gt, Le, Lt
from fakes import FakeMlx, FakeOnnxRuntime, Feed, Store, clean_chunk, tone

from readily_engine.catalog import CatalogEntry, load_manifest
from readily_engine.generation import DegenerateDraw, GenerationRecord
from readily_engine.loading import LazySynthesizer, synthesizer_for
from readily_engine.loading.parameters import ParameterMismatch
from readily_engine.loading.registry import architecture_named

# A rate the MLX fake reports that no Architecture declares.
UNDECLARED_RATE = 22_050

# Texts of growing length, for the runaway bound.
TEXTS = (
    "Hi.",
    "The lamp was lit before the rain began.",
    "The lamp was lit before the rain began, and by the time the last train "
    "had gone the whole street was dark except for that one window, where "
    "someone sat reading.",
)


class Backends:
    """Both Backends' fakes, installed together, so a draw is scripted the
    same way whichever one the Architecture loads."""

    def __init__(
        self, promoted: Path, graphs: Mapping[str, Callable[[Feed], list[np.ndarray]]]
    ):
        self.onnx = FakeOnnxRuntime(promoted, graphs)
        self.mlx = FakeMlx()

    def speak(self, *chunks: np.ndarray) -> None:
        """Make every draw from here on the concatenation of `chunks`."""
        self.onnx.speak(chunks)
        self.mlx.speak(chunks)

    @property
    def loaded(self) -> bool:
        return bool(self.onnx.paths or self.mlx.loaded)


class Conformance:
    # The committed Catalog entry to run, and the rate its audio comes at:
    # None for an Architecture that passes on whatever rate its Backend
    # reports.
    model_id: ClassVar[str]
    sample_rate: ClassVar[int | None]
    # Answers for the named ONNX graphs; any other answers with the draw.
    graphs: ClassVar[Mapping[str, Callable[[Feed], list[np.ndarray]]]] = {}
    # Whether a draw that never ends is cut at a budget the text earns. An
    # Architecture whose graphs return one finished draw has no runaway to
    # cut, says so here, and must hand back the whole draw.
    runaway_budget: ClassVar[bool] = True

    def promote(self, model_dir: Path, entry: CatalogEntry) -> None:
        """Write the sidecar files `load` parses into `model_dir`."""

    @pytest.fixture
    def entry(self) -> CatalogEntry:
        entry = load_manifest().find(self.model_id)
        assert entry is not None, f"no committed entry {self.model_id}"
        return entry

    @pytest.fixture
    def backends(self, monkeypatch, tmp_path) -> Backends:
        def refuse(*_args, **_kwargs):
            raise AssertionError("an Architecture opened a socket")

        monkeypatch.setattr("socket.socket", refuse)
        backends = Backends(tmp_path, self.graphs)
        backends.onnx.install(monkeypatch)
        backends.mlx.install(monkeypatch)
        return backends

    @pytest.fixture
    def synthesizer(self, entry, backends, tmp_path) -> LazySynthesizer:
        store = Store(tmp_path)
        model_dir = store.promoted_dir(entry)
        for path in architecture_named(entry.architecture).expected_files:
            (model_dir / path).parent.mkdir(parents=True, exist_ok=True)
            (model_dir / path).write_bytes(b"placeholder")
        self.promote(model_dir, entry)
        return synthesizer_for(entry, store)

    def test_a_directory_the_store_has_not_promoted_is_never_loaded(
        self, entry, backends, tmp_path
    ):
        synthesizer = synthesizer_for(entry, Store(tmp_path))

        assert synthesizer.prewarm() is False
        with pytest.raises(FileNotFoundError):
            synthesizer.generate(synthesizer.warmup)
        assert not backends.loaded

    def test_a_voice_the_entry_does_not_offer_is_refused(self, synthesizer):
        with pytest.raises(ValueError):
            synthesizer.generate(replace(synthesizer.warmup, voice_id="no-such-voice"))

    def test_audio_comes_at_the_declared_sample_rate(self, synthesizer, backends):
        # The Backend reports a rate no Architecture declares, so one that
        # stamps its own rate over the Backend's is caught either way.
        backends.mlx.model.sample_rate = UNDECLARED_RATE
        backends.speak(tone(400, 1.0))

        audio = synthesizer.generate(synthesizer.warmup)

        assert audio.sample_rate == (self.sample_rate or UNDECLARED_RATE)
        assert len(audio.pcm)

    def test_a_silent_draw_is_degenerate(self, synthesizer, backends):
        backends.speak(np.zeros(240_000, dtype=np.float32))

        with pytest.raises(DegenerateDraw):
            synthesizer.generate(synthesizer.warmup)

    def test_a_longer_text_is_never_cut_shorter(self, synthesizer, backends):
        # ADR 0014 keeps the runaway budget inside the Architecture, so it is
        # held here by what it does: a draw that never ends is cut, and a
        # longer text earns a longer draw before it is. One without a budget
        # cuts nothing.
        draw = [clean_chunk()] * 60
        backends.speak(*draw)

        audios = [
            synthesizer.generate(replace(synthesizer.warmup, text=text))
            for text in TEXTS
        ]
        seconds = [len(audio.pcm) / audio.sample_rate for audio in audios]

        drawn = sum(map(len, draw)) / audios[0].sample_rate
        if self.runaway_budget:
            assert all(cut < drawn for cut in seconds)
            assert all(a < b for a, b in pairwise(seconds))
        else:
            assert seconds == [drawn] * len(TEXTS)

    def test_the_warm_up_record_is_one_a_narration_could_store(
        self, synthesizer, backends, entry
    ):
        architecture = architecture_named(entry.architecture)
        warmup = synthesizer.warmup

        assert warmup.voice_id == entry.default_voice
        assert warmup.text == architecture.warmup_text
        assert GenerationRecord.from_json(warmup.canonical_json()) == warmup
        architecture.parameters.model_validate(warmup.parameters)
        assert synthesizer.prewarm() is True
        assert backends.loaded

    def test_committed_knobs_round_trip_through_the_schema(self, entry):
        # The Record carries a plain mapping and `generate` parses it with
        # the Architecture's schema, so what the Catalog pins, on every entry
        # the Architecture runs, must survive the Record's JSON and the
        # schema unchanged, byte for byte.
        schema = architecture_named(entry.architecture).parameters
        for pinned in load_manifest().models:
            if pinned.architecture != entry.architecture:
                continue
            record = GenerationRecord.for_entry(pinned, pinned.default_voice, "Hi.")
            restored = GenerationRecord.from_json(record.canonical_json())

            parsed = schema.model_validate(restored.parameters)

            assert json.dumps(
                parsed.model_dump(exclude_none=True), sort_keys=True
            ) == json.dumps(pinned.generation_parameters, sort_keys=True), pinned.id

    @pytest.mark.parametrize("value", ["off-schema", "out-of-range"])
    def test_a_knob_the_schema_would_refuse_is_refused_at_boot(
        self, entry, tmp_path, value
    ):
        schema = architecture_named(entry.architecture).parameters
        pinned = entry.generation_parameters
        refused = (
            [{**pinned, "no_such_knob": 1}]
            if value == "off-schema"
            else [
                {**pinned, name: bad}
                for name, field in schema.model_fields.items()
                for bad in _beyond(field.metadata, field.annotation)
            ]
        )
        for knobs in refused:
            wrong = entry.model_copy(update={"generation_parameters": knobs})
            with pytest.raises(ParameterMismatch):
                synthesizer_for(wrong, Store(tmp_path))


def _beyond(bounds: list[object], annotation: object) -> list[float]:
    """One value past each bound a schema field declares."""
    step = 1 if int in (getattr(annotation, "__args__", ()) or (annotation,)) else 0.5
    values = []
    for bound in bounds:
        match bound:
            case Ge(ge=limit):
                values.append(limit - step)
            case Gt(gt=limit):
                values.append(limit)
            case Le(le=limit):
                values.append(limit + step)
            case Lt(lt=limit):
                values.append(limit)
    return values
