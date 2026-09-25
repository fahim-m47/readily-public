"""Simple mode admits only the exact recipe qualified for a Voice."""

import pytest

from readily_engine.catalog import load_manifest
from readily_engine.catalog.recipes import qualified, resolve_simple
from readily_engine.generation import GenerationRecord
from readily_engine.narration.admission import require_simple_plan


def test_simple_uses_qualified_defaults_and_shares_advanced_segment_identity():
    entry = load_manifest().resolve("kokoro:82m")
    voice = entry.default_voice
    simple = resolve_simple(entry, voice)
    advanced = entry.compose({})
    assert GenerationRecord.for_entry(simple.entry, voice, "Hello.").key == (
        GenerationRecord.for_entry(advanced.entry, voice, "Hello.").key
    )
    assert qualified(entry, voice)
    changed = entry.model_copy(update={"version": entry.version + 1})
    assert not qualified(changed, voice)
    with pytest.raises(ValueError, match="qualified"):
        resolve_simple(changed, voice)


def test_qwen_stays_advanced_until_its_recipe_passes():
    entry = load_manifest().resolve("qwen3-tts:0.6b")
    assert not qualified(entry, entry.default_voice)
    with pytest.raises(ValueError, match="qualified"):
        resolve_simple(entry, entry.default_voice)


def test_simple_ignores_overrides_and_refuses_an_advanced_history_recipe(tmp_path):
    from conftest import wait_until
    from storage_fakes import open_test_storage
    from worker_fakes import RecordingPlayback, RecordingSynthesizer, voice_model

    from readily_engine.narration.worker import GenerationWorker, NarrationRequest

    entry = load_manifest().resolve("supertonic:66m")
    voice = entry.default_voice
    storage = open_test_storage(tmp_path)
    storage.set_control_overrides(entry, voice, {"steps": 12, "seed": 42})
    synth = RecordingSynthesizer()
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=voice,
    )
    try:
        simple = worker.start(
            NarrationRequest(entry.id, "Hello.", voice, mode="simple")
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        record = storage.plan(simple).segments[0].generation
        assert record.parameters["steps"] == entry.generation_parameters["steps"]
        assert record.seed is None
        assert storage.effective_controls(entry, voice).seed == 42
        advanced = worker.start(NarrationRequest(entry.id, "Hello.", voice))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        with pytest.raises(ValueError, match="qualified"):
            worker.resume(advanced, mode="simple")
        worker.resume(simple, mode="simple")
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert synth.inputs == ["Hello.", "Hello."]
    finally:
        worker.close()
        storage.close()


def test_simple_prepares_the_whole_narration_whatever_the_overrides_say(tmp_path):
    """Prepare-first is the shipped default, and Simple never reads a
    Voice's overrides, so an Advanced reader who turned it off has not
    turned it off for Simple."""
    from conftest import wait_until
    from storage_fakes import open_test_storage
    from worker_fakes import FixedSynthesizer, RecordingPlayback, voice_model

    from readily_engine.narration.worker import GenerationWorker, NarrationRequest

    playback = RecordingPlayback()

    class ObservePlayback(FixedSynthesizer):
        def generate(self, record):
            assert playback.played == []
            return super().generate(record)

    entry = load_manifest().resolve("kokoro:82m")
    voice = entry.default_voice
    storage = open_test_storage(tmp_path)
    storage.set_control_overrides(entry, voice, {"prepare_first": False})
    synth = ObservePlayback(20.0)
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry)},
        playback,
        storage,
        default_model=entry.id,
        default_voice=voice,
    )
    try:
        narration_id = worker.start(
            NarrationRequest(entry.id, "First. Second. Third.", voice, mode="simple")
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert len(synth.inputs) > 1
        assert playback.played
        assert storage.plan(narration_id).settings.prepare_first is True
    finally:
        worker.close()
        storage.close()


def test_failed_blocks_retry_once_and_report_the_gap(tmp_path):
    from conftest import wait_until
    from storage_fakes import open_test_storage
    from worker_fakes import RecordingPlayback, voice_model

    from readily_engine.narration.worker import GenerationWorker, NarrationRequest

    class Fails:
        calls = 0

        def generate(self, record):
            self.calls += 1
            raise ValueError("cannot speak")

    entry = load_manifest().resolve("kokoro:82m")
    synth = Fails()
    playback = RecordingPlayback()
    storage = open_test_storage(tmp_path)
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry)},
        playback,
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        narration = worker.start(
            NarrationRequest(entry.id, "Missing words.", entry.default_voice, "simple")
        )
        wait_until(lambda: worker.snapshot()["phase"] in {"finished", "failed"})
        assert synth.calls == 2
        assert storage.history_detail(narration).gaps[0].source_start == 0
        assert worker.snapshot()["error"]["code"] == "generation_gap"
        assert not playback.played
        assert worker.snapshot()["voiceId"] == entry.default_voice
        assert worker.snapshot()["speed"] == 1
    finally:
        worker.close()
        storage.close()


def test_successful_retry_redraws_and_stays_admitted_in_simple_mode(tmp_path):
    from conftest import wait_until
    from storage_fakes import open_test_storage
    from worker_fakes import RecordingPlayback, RecordingSynthesizer, voice_model

    from readily_engine.narration.worker import GenerationWorker, NarrationRequest

    class Retry(RecordingSynthesizer):
        def __init__(self):
            super().__init__()
            self.records = []

        def generate(self, record):
            self.records.append(record)
            if len(self.records) == 1:
                raise ValueError("transient failure")
            return super().generate(record)

    entry = load_manifest().resolve("kokoro:82m")
    synth = Retry()
    storage = open_test_storage(tmp_path)
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        narration = worker.start(
            NarrationRequest(entry.id, "Hello.", entry.default_voice, "simple")
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        base = GenerationRecord.for_entry(entry, entry.default_voice, "Hello.")
        assert synth.records == [base, base.redraw()]
        assert storage.plan(narration).segments[0].generation == base.redraw()
        assert not storage.history_detail(narration).gaps
        worker.resume(narration, mode="simple")
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert len(synth.records) == 2
        again = worker.start(
            NarrationRequest(entry.id, "Hello.", entry.default_voice, "simple")
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert len(synth.records) == 2
        assert storage.plan(again).segments[0].generation == base.redraw()
    finally:
        worker.close()
        storage.close()


def test_simple_admission_ignores_the_draw_and_word_timing(tmp_path):
    from dataclasses import replace

    from conftest import wait_until
    from storage_fakes import open_test_storage
    from worker_fakes import RecordingPlayback, RecordingSynthesizer, voice_model

    from readily_engine.narration.worker import GenerationWorker, NarrationRequest

    entry = load_manifest().resolve("kokoro:82m")
    voice = entry.default_voice
    storage = open_test_storage(tmp_path)
    worker = GenerationWorker(
        {entry.id: voice_model(RecordingSynthesizer(), entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=voice,
    )
    try:
        narration = worker.start(NarrationRequest(entry.id, "Hello.", voice, "simple"))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        segment = storage.plan(narration).segments[0]
        segment = storage.rekey_segment(
            narration, segment, replace(segment.generation, word_timing="other")
        )
        require_simple_plan(entry, storage.plan(narration))
        segment = storage.rekey_segment(
            narration, segment, replace(segment.generation, seed=7)
        )
        require_simple_plan(entry, storage.plan(narration))
        storage.rekey_segment(
            narration, segment, replace(segment.generation, text="Goodbye.")
        )
        with pytest.raises(ValueError, match="qualified"):
            require_simple_plan(entry, storage.plan(narration))
    finally:
        worker.close()
        storage.close()
