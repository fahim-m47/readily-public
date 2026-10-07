"""Simple mode narrates any Voice with its Catalog defaults."""

from readily_engine.catalog import load_manifest
from readily_engine.catalog.recipes import qualified, resolve_simple
from readily_engine.generation import GenerationRecord


def test_simple_uses_catalog_defaults_and_shares_advanced_segment_identity():
    entry = load_manifest().resolve("kokoro:82m")
    voice = entry.default_voice
    simple = resolve_simple(entry)
    advanced = entry.compose({})
    assert GenerationRecord.for_entry(simple.entry, voice, "Hello.").key == (
        GenerationRecord.for_entry(advanced.entry, voice, "Hello.").key
    )
    assert qualified(entry, voice)
    changed = entry.model_copy(update={"version": entry.version + 1})
    assert not qualified(changed, voice)


def test_simple_resolves_a_voice_no_curator_has_qualified():
    entry = load_manifest().resolve("qwen3-tts:0.6b")
    assert not qualified(entry, entry.default_voice)
    assert resolve_simple(entry) == entry.compose({})


def test_simple_ignores_overrides_that_advanced_applies(tmp_path):
    from conftest import wait_until
    from storage_fakes import open_test_storage
    from worker_fakes import RecordingPlayback, RecordingSynthesizer, voice_model

    from readily_engine.narration.worker import GenerationWorker, NarrationRequest

    entry = load_manifest().resolve("chatterbox:turbo")
    voice = entry.default_voice
    storage = open_test_storage(tmp_path)
    storage.set_control_overrides(entry, voice, {"temperature": 0.5, "seed": 42})
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
        assert (
            record.parameters["temperature"]
            == entry.generation_parameters["temperature"]
        )
        assert record.seed is None
        assert storage.effective_controls(entry, voice).seed == 42
        worker.start(NarrationRequest(entry.id, "Hello.", voice))
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


def test_successful_retry_redraws_and_replays_from_the_cache(tmp_path):
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
        worker.resume(narration)
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
