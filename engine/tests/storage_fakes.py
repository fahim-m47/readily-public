"""Deterministic NumPy-backed codec for cross-platform storage tests."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from generation_fakes import entry_for

from readily_engine.audio import FloatPcm
from readily_engine.catalog import PausePolicy
from readily_engine.chunking import Block, Boundary, ChunkedSource
from readily_engine.narration.measure import measure, trimmed_audio
from readily_engine.storage.history import HistoryStore, SynthesisSettings
from readily_engine.storage.segments import SegmentStore, StoredAudio
from readily_engine.storage.storage import (
    NarrationPlan,
    NarrationStorage,
    PlannedSegment,
)

# The settings a test Narration is planned with. The pauses are the test
# Catalog entry's (`conftest.ENTRY`), so what a test plans and what its
# Assembler expects agree without either naming a number.
SETTINGS = SynthesisSettings(
    "acme:1m",
    1,
    "narrator",
    1.0,
    PausePolicy(pause_sentence_ms=80, pause_paragraph_break_ms=400),
    # Streaming, like every worker test: prepare-first has its own tests.
    prepare_first=False,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class AdvancingClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def encode_npz(pcm: FloatPcm, sample_rate: int, path: Path) -> None:
    with path.open("wb") as handle:
        np.savez(
            handle,
            pcm=np.asarray(pcm, dtype=np.float32),
            sample_rate=np.array([sample_rate], dtype=np.int64),
        )


def decode_npz(path: Path) -> StoredAudio:
    with np.load(path) as contents:
        return StoredAudio(
            pcm=np.asarray(contents["pcm"], dtype=np.float32).copy(),
            sample_rate=int(contents["sample_rate"][0]),
        )


def open_test_storage(
    root: Path,
    clock: Callable[[], datetime] = utc_now,
) -> NarrationStorage:
    return NarrationStorage(
        HistoryStore.open(root / "readily.db", clock=clock),
        SegmentStore(root / "segments", encoder=encode_npz, decoder=decode_npz),
        clock=clock,
    )


def strip_generation_records(database: Path) -> None:
    """Age every Segment in a History database back to before Generation
    Records existed, which no supported write path can produce any more."""
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE narration_segments SET generation_json = NULL")


def create_plan(storage: NarrationStorage, text: str) -> NarrationPlan:
    return storage.create(
        ChunkedSource(
            source=text,
            blocks=(Block(text, 0, len(text), Boundary.PARAGRAPH),),
        ),
        SETTINGS,
        entry_for(SETTINGS),
    )


def publish_segment(
    storage: NarrationStorage,
    plan: NarrationPlan,
    segment: PlannedSegment,
    pcm: FloatPcm,
    sample_rate: int,
) -> StoredAudio:
    """Store and measure one Block the way the worker does after synthesis,
    returning the trimmed audio playback would feed."""
    raw = storage.store_audio(segment.key, pcm, sample_rate)
    measured = measure(storage, plan, segment, raw)
    audio = trimmed_audio(storage, plan, measured)
    assert audio is not None
    return audio


def publish_one(storage: NarrationStorage, plan: NarrationPlan) -> StoredAudio:
    """Give a single-Block plan its audio — the setup most retention tests
    need before they have anything to evict."""
    return publish_segment(
        storage, plan, plan.segments[0], np.ones(240, dtype=np.float32), 24_000
    )
