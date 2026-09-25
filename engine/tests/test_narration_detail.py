"""A detail read past a cursor reads word timings only for the Blocks returned."""

import numpy as np
from generation_fakes import entry_for
from storage_fakes import SETTINGS, open_test_storage
from worker_fakes import KOKORO

from readily_engine.chunking import chunk
from readily_engine.narration.measure import measure
from readily_engine.narration.narrator import EngineNarrator


class ReadOnlyNarrator(EngineNarrator):
    def __init__(self, storage):
        self._storage = storage


class TimingReads:
    """The storage, with the ordinal of every Block whose timings were read."""

    def __init__(self, storage):
        self._storage = storage
        self.ordinals: list[int] = []

    def timings(self, segment):
        self.ordinals.append(segment.ordinal)
        return self._storage.timings(segment)

    def __getattr__(self, name):
        return getattr(self._storage, name)


def test_detail_after_an_ordinal_reads_timings_only_for_the_blocks_returned(tmp_path):
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("First.\n\nSecond.\n\nThird.", KOKORO), SETTINGS, entry_for(SETTINGS)
    )
    for segment in plan.segments:
        raw = storage.store_audio(segment.key, np.ones(2000, np.float32), 1000)
        measure(storage, plan, segment, raw)
    reads = TimingReads(storage)

    tail = ReadOnlyNarrator(reads).detail(plan.id, after_ordinal=1)

    assert [part.ordinal for part in tail.segments] == [2]
    assert tail.segments[0].timeline_start_sec is not None
    assert reads.ordinals == [2]


def test_detail_after_an_ordinal_answers_whole_once_a_block_before_it_is_unmeasured(
    tmp_path,
):
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        chunk("First.\n\nSecond.\n\nThird.", KOKORO), SETTINGS, entry_for(SETTINGS)
    )
    for segment in plan.segments:
        raw = storage.store_audio(segment.key, np.ones(2000, np.float32), 1000)
        measure(storage, plan, segment, raw)
    storage.update_retention(segment_budget_bytes=1, keep_audio_days=None)
    storage.store_audio(plan.segments[0].key, np.ones(4000, np.float32), 1000)

    whole = ReadOnlyNarrator(storage).detail(plan.id, after_ordinal=1)

    assert [part.ordinal for part in whole.segments] == [0, 1, 2]
    assert whole.segments[0].duration_sec is None
