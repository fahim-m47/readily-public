"""The Narrator preserves synthesis times and only aligns eligible Segments."""

import numpy as np
import pytest
from conftest import wait_until
from storage_fakes import open_test_storage
from worker_fakes import RecordingPlayback, voice_model

from readily_engine.catalog import load_manifest
from readily_engine.generation import GeneratedAudio
from readily_engine.narration.worker import GenerationWorker, NarrationRequest
from readily_engine.timings import Timing


@pytest.mark.parametrize(
    "native,returned,disabled,raises,expected,calls",
    [
        (True, True, False, False, "spoken", 0),
        (True, False, False, False, None, 0),
        (False, False, False, False, "matched", 1),
        (False, False, True, False, None, 0),
        (False, False, False, True, None, 1),
        (False, True, False, False, "matched", 0),
        (False, "spoken", False, False, None, 0),
    ],
)
def test_worker_routes_timings_and_preserves_audio_on_alignment_failure(
    tmp_path, native, returned, disabled, raises, expected, calls
):
    entry = load_manifest().find("supertonic:66m")
    entry = entry.model_copy(
        update={"word_timing_languages": ("en",) if native else ()}
    )
    storage = open_test_storage(tmp_path)
    timing = Timing(
        start_char=0,
        end_char=5,
        start_sec=0.1,
        end_sec=0.3,
        provenance="spoken" if native or returned == "spoken" else "matched",
    )
    pcm = np.r_[np.zeros(1000), np.ones(4000), np.zeros(1000)].astype(np.float32)

    class Synthesizer:
        def generate(self, record):
            storage.set_control_overrides(
                entry,
                entry.default_voice,
                {"word_timing": "wav2vec2:base-960h" if disabled else "off"},
            )
            return GeneratedAudio(pcm, 10000, (timing,) if returned else ())

    class Aligner:
        calls = 0

        def align(self, text, audio, sample_rate):
            self.calls += 1
            if raises:
                raise RuntimeError("bad alignment")
            return (timing,)

    storage.set_control_overrides(
        entry,
        entry.default_voice,
        {"word_timing": "off" if disabled else "wav2vec2:base-960h"},
    )
    aligner = Aligner()
    worker = GenerationWorker(
        {entry.id: voice_model(Synthesizer(), entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
        aligners={"wav2vec2:base-960h": aligner},
    )
    try:
        narration = worker.start(
            NarrationRequest(model=entry.id, voice=entry.default_voice, input="Hello.")
        )
        wait_until(lambda: worker.snapshot()["phase"] in {"finished", "failed"})
        assert worker.snapshot()["phase"] == "finished"
        segment = storage.plan(narration).segments[0]
        raw = storage.raw_audio(segment)
        assert raw is not None
        assert len(raw.pcm) == len(pcm)
        assert aligner.calls == calls
        assert [word.provenance for word in raw.timings] == (
            [expected] if expected else []
        )
        if calls and not raises:
            assert raw.timings[0].start_sec > timing.start_sec
    finally:
        worker.close()
        storage.close()


def test_the_word_route_follows_the_voice_language_and_the_frozen_choice():
    from readily_engine.generation import GenerationRecord
    from readily_engine.narration.words import word_route

    catalog = load_manifest()
    kokoro = catalog.find("kokoro:82m")
    qwen = catalog.find("qwen3-tts:0.6b")
    aligned = qwen.compose({"word_timing": "wav2vec2:base-960h"}).entry

    def route(entry):
        record = GenerationRecord.for_entry(entry, entry.default_voice, "Hi.")
        return word_route(entry, record)

    assert route(kokoro) == "spoken"
    assert route(qwen) == "estimated"
    assert route(aligned) == "matched"
    foreign = kokoro.model_copy(update={"word_timing_languages": ("fr",)})
    assert route(foreign) == "estimated"
