"""Source-coordinate seeking through the Narrator's public worker interface."""

import threading

import numpy as np
import pytest
from conftest import wait_until
from generation_fakes import entry_for
from storage_fakes import SETTINGS, open_test_storage
from worker_fakes import KOKORO, RecordingPlayback, RecordingSynthesizer, worker_for

from readily_engine.chunking import chunk
from readily_engine.generation import GeneratedAudio
from readily_engine.narration.measure import measure
from readily_engine.narration.narrator import EngineNarrator
from readily_engine.storage.history import NarrationStatus
from readily_engine.timings import Timing


class Held:
    """A fake that blocks inside one call until the test lets it go."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_for_release(self):
        self.entered.set()
        self.release.wait(timeout=3)


class HoldFirstFeed(RecordingPlayback, Held):
    def __init__(self):
        RecordingPlayback.__init__(self)
        Held.__init__(self)

    def feed(self, pcm, sample_rate, generation, *, pause_frames=0):
        if generation == 1:
            self.wait_for_release()
        return super().feed(pcm, sample_rate, generation, pause_frames=pause_frames)


class ReadOnlyNarrator(EngineNarrator):
    def __init__(self, storage):
        self._storage = storage


def word(start_char, end_char, start_sec, end_sec, provenance="matched"):
    return Timing(
        start_char=start_char,
        end_char=end_char,
        start_sec=start_sec,
        end_sec=end_sec,
        provenance=provenance,
    )


def stored(storage, plan, segment, pcm, timings=()):
    """Store and measure one Block, as generation would have."""
    raw = storage.store_audio(segment.key, pcm, 1000, timings=timings)
    measure(storage, plan, segment, raw)


def played_after_seek(storage, plan, source_offset, synth=None, *, holds=()):
    """Resume the stopped Narration, seek while its first Block is held in
    playback (and inside every other `Held` fake), and return what played."""
    storage.set_status(plan.id, NarrationStatus.STOPPED)
    playback = HoldFirstFeed()
    worker = worker_for(synth or RecordingSynthesizer(), playback, storage)
    held = (playback, *holds)
    try:
        worker.resume(plan.id)
        for hold in held:
            assert hold.entered.wait(timeout=2)
        assert worker.seek(source_offset)
        for hold in held:
            hold.release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        return np.concatenate([pcm for pcm, _ in playback.played])
    finally:
        for hold in held:
            hold.release.set()
        worker.close()


@pytest.mark.parametrize(
    "provenance, expected_frames",
    [("spoken", 1050), ("matched", 1150), ("estimated", 1250), (None, 1250)],
)
def test_word_seek_in_a_cached_block_uses_timing_and_fades_in(
    tmp_path, provenance, expected_frames
):
    storage = open_test_storage(tmp_path)
    plan = storage.create(chunk("Hello world.", KOKORO), SETTINGS, entry_for(SETTINGS))
    timings = () if provenance is None else (word(6, 11, 1, 1.5, provenance),)
    stored(storage, plan, plan.segments[0], np.ones(2000, np.float32), timings)
    synth = RecordingSynthesizer()
    pcm = played_after_seek(storage, plan, 8, synth)
    assert synth.inputs == []
    assert len(pcm) == expected_frames
    assert pcm[0] == 0
    assert pcm[20] == pytest.approx(1)
    assert storage.plan(plan.id).playhead_sec == pytest.approx(2)


@pytest.mark.parametrize("with_timing, expected_frames", [(True, 1150), (False, 1250)])
def test_uncached_word_is_synthesized_before_resolving_its_position(
    tmp_path, with_timing, expected_frames
):
    class TimedSynthesizer(RecordingSynthesizer, Held):
        def __init__(self):
            RecordingSynthesizer.__init__(self)
            Held.__init__(self)

        def generate(self, record):
            self.inputs.append(record.text)
            self.wait_for_release()
            return GeneratedAudio(
                np.ones(2000, np.float32),
                1000,
                (word(6, 11, 1, 1.5),) if with_timing else (),
            )

    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("First.\n\nHello world.", KOKORO), SETTINGS, entry_for(SETTINGS)
    )
    stored(storage, plan, plan.segments[0], np.ones(1000, np.float32))
    synth = TimedSynthesizer()
    pcm = played_after_seek(
        storage, plan, plan.segments[1].source_start + 8, synth, holds=(synth,)
    )
    assert set(synth.inputs) == {"Hello world."}
    assert len(pcm) == expected_frames
    assert storage.plan(plan.id).playhead_sec == pytest.approx(3.4)


def test_shared_segment_maps_each_sources_range_and_trim(tmp_path):
    storage = open_test_storage(tmp_path)
    plans = [
        storage.create(chunk(source, KOKORO), SETTINGS, entry_for(SETTINGS))
        for source in ("Hello world.", "🌊 First.\n\nHello world.")
    ]
    assert plans[0].segments[-1].key == plans[1].segments[-1].key
    for plan in plans:
        for segment in plan.segments:
            if segment.text == "Hello world.":
                pcm = np.concatenate(
                    [np.zeros(500, np.float32), np.ones(2000, np.float32)]
                )
                stored(storage, plan, segment, pcm, (word(6, 11, 1.5, 2),))
            else:
                stored(storage, plan, segment, np.ones(1000, np.float32))
        synth = RecordingSynthesizer()
        pcm = played_after_seek(storage, plan, plan.source.index("world") + 4, synth)
        assert len(pcm) == 1150
        assert synth.inputs == []


@pytest.mark.parametrize("kind", ["repeated", "outside-text", "outside-audio"])
def test_invalid_word_positions_are_estimated_inside_the_block(tmp_path, kind):
    storage = open_test_storage(tmp_path)
    plan = storage.create(chunk("Hello world.", KOKORO), SETTINGS, entry_for(SETTINGS))
    timing = word(6, 11, 1, 1.5)
    timings = {
        "repeated": (timing, timing),
        "outside-text": (timing.model_copy(update={"end_char": 99}),),
        "outside-audio": (timing.model_copy(update={"end_sec": 99}),),
    }[kind]
    stored(storage, plan, plan.segments[0], np.ones(2000, np.float32), timings)
    assert len(played_after_seek(storage, plan, 8)) == 1250


@pytest.mark.parametrize("unordered", [False, True])
def test_read_along_uses_the_same_source_ranges_and_trim_as_seek(tmp_path, unordered):
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("🌊 First.\n\nHello world.", KOKORO), SETTINGS, entry_for(SETTINGS)
    )
    for segment in plan.segments:
        timings = (
            ()
            if segment.ordinal == 0
            else (word(0, 5, 0.6, 1, "spoken"), word(6, 11, 1.5, 2))
        )
        if unordered:
            timings = tuple(reversed(timings))
        pcm = np.concatenate([np.zeros(500, np.float32), np.ones(2000, np.float32)])
        stored(storage, plan, segment, pcm, timings)
    words = ReadOnlyNarrator(storage).detail(plan.id).segments[1].timings
    assert [plan.source[w.source_start : w.source_end] for w in words] == [
        "Hello",
        "world",
    ]
    assert words[1].start_sec == pytest.approx(3.48)
    assert words[1].end_sec == pytest.approx(3.98)


def test_missing_words_use_neighbouring_anchors_and_character_weights(tmp_path):
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("First a bee last.", KOKORO), SETTINGS, entry_for(SETTINGS)
    )
    stored(
        storage,
        plan,
        plan.segments[0],
        np.ones(6000, np.float32),
        (word(0, 5, 0, 1, "spoken"), word(12, 16, 5, 6)),
    )
    words = ReadOnlyNarrator(storage).detail(plan.id).segments[0].timings
    assert [(w.start_sec, w.end_sec, w.provenance) for w in words] == [
        (0, 1, "spoken"),
        (1, 2, "estimated"),
        (2, 5, "estimated"),
        (5, 6, "matched"),
    ]
    assert len(played_after_seek(storage, plan, plan.source.index("bee"))) == 4250


@pytest.mark.parametrize("with_timing", [False, True])
def test_first_word_survives_trimming_and_seeks_to_block_start(tmp_path, with_timing):
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("  Hello world.", KOKORO), SETTINGS, entry_for(SETTINGS)
    )
    stored(
        storage,
        plan,
        plan.segments[0],
        np.concatenate([np.zeros(500, np.float32), np.ones(2000, np.float32)]),
        (word(0, 5, 0.1, 0.6, "spoken"),) if with_timing else (),
    )
    first = ReadOnlyNarrator(storage).detail(plan.id).segments[0].timings[0]
    assert plan.source[first.source_start : first.source_end] == "Hello"
    assert first.start_sec == 0
    assert first.provenance == ("spoken" if with_timing else "estimated")
    assert len(played_after_seek(storage, plan, plan.source.index("Hello"))) == 2040


@pytest.mark.parametrize(
    "reports, mapped", [(True, True), (True, False), (False, True)]
)
def test_worker_obeys_word_time_capability_and_logs_block_fallback(
    tmp_path, caplog, reports, mapped
):
    from conftest import make_entry
    from worker_fakes import request, voice_model

    from readily_engine.narration.words import words_for
    from readily_engine.narration.worker import GenerationWorker

    class Synthesizer:
        def generate(self, record):
            return GeneratedAudio(
                np.ones(24000, np.float32),
                24000,
                (word(0, 5, 0.2, 0.6, "spoken"),) if mapped else (),
            )

    storage = open_test_storage(tmp_path)
    entry = make_entry(word_timing_languages=["en"] if reports else [])
    worker = GenerationWorker(
        {entry.id: voice_model(Synthesizer(), entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        narration_id = worker.start(request("Hello"))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        plan = storage.plan(narration_id)
        words = words_for(storage, plan, plan.segments[0], 0)
        assert [w.provenance for w in words] == [
            "spoken" if reports and mapped else "estimated"
        ]
        if reports and not mapped:
            assert "Block 0" in caplog.text
            assert narration_id in caplog.text
            assert "estimated" in caplog.text
        else:
            assert "word timing mapping failed" not in caplog.text
    finally:
        worker.close()


def test_spoken_word_overlapping_trim_keeps_its_onset_and_clips_its_end():
    from readily_engine.timings import place_timings

    words = place_timings(
        (word(0, 5, 0.2, 1.1, "spoken"),),
        text="Hello",
        source_start=0,
        trim_start_sec=0.1,
        duration_sec=0.8,
        timeline_start_sec=0,
    )
    assert len(words) == 1
    assert words[0].provenance == "spoken"
    assert words[0].start_sec == pytest.approx(0.1)
    assert words[0].end_sec == pytest.approx(0.8)


def test_wholly_trimmed_first_word_retains_spoken_provenance_at_block_start():
    from readily_engine.timings import place_timings

    words = place_timings(
        (word(0, 5, 0.02, 0.08, "spoken"), word(6, 11, 0.3, 0.5, "spoken")),
        text="Hello world",
        source_start=0,
        trim_start_sec=0.1,
        duration_sec=0.8,
        timeline_start_sec=5,
    )
    assert words[0].provenance == "spoken"
    assert words[0].start_sec == words[0].end_sec == 5
