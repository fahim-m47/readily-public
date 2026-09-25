"""Measure a Block once; slice it by the stored range ever after.

The trimmed range depends on the Pause Policy's margin and on the
neighbouring boundaries (a mid-sentence seam keeps no margin on its side),
so storage cannot know it from the audio alone. This module owns that
derivation: the first read of a Block trims it and records the range in
History, and every later read slices the raw audio without decoding twice.
"""

from collections.abc import Iterable
from dataclasses import replace

from readily_engine.audio import FloatPcm
from readily_engine.chunking import Boundary
from readily_engine.narration import assembly
from readily_engine.storage.history import SegmentRange
from readily_engine.storage.segments import StoredAudio
from readily_engine.storage.storage import (
    NarrationPlan,
    NarrationStorage,
    PlannedSegment,
)


def trim_margins(plan: NarrationPlan, segment: PlannedSegment) -> tuple[int, int]:
    """The (lead, tail) margins this Block keeps, in milliseconds."""
    previous = plan.segments[segment.ordinal - 1] if segment.ordinal else None
    return assembly.trim_margins(
        plan.settings.pause_policy,
        None if previous is None else Boundary(previous.boundary),
        Boundary(segment.boundary),
    )


def trim_range(
    plan: NarrationPlan, segment: PlannedSegment, pcm: FloatPcm, sample_rate: int
) -> tuple[int, int]:
    """The raw frame range shared by word alignment and playback measurement."""
    lead, tail = trim_margins(plan, segment)
    return assembly.trim_range(
        pcm, sample_rate, lead_margin_ms=lead, tail_margin_ms=tail
    )


def measure(
    storage: NarrationStorage,
    plan: NarrationPlan,
    segment: PlannedSegment,
    raw: StoredAudio,
) -> PlannedSegment:
    """Trim raw audio, record the range, and return the measured Block."""
    start, end = trim_range(plan, segment, raw.pcm, raw.sample_rate)
    storage.record_length(
        plan.id,
        segment.ordinal,
        key=segment.key,
        sample_rate=raw.sample_rate,
        frame_count=end - start,
        trim_start=start,
    )
    return replace(
        segment,
        sample_rate=raw.sample_rate,
        frame_count=end - start,
        trim_start=start,
    )


def cached_block(
    storage: NarrationStorage, plan: NarrationPlan, segment: PlannedSegment
) -> PlannedSegment | None:
    """The measured Block its cached audio replays, or None without audio.

    The range comes from History, never from the Block handed in: a plan
    is a snapshot, and regenerating a shared key clears ranges under it.
    A Block History has measured costs one point read; only an unmeasured
    one pays for a decode.
    """
    stored = storage.segment_range(plan.id, segment.ordinal, segment.key)
    if stored is not None and storage.has_verified_audio(segment):
        return _ranged(segment, stored)
    raw = storage.raw_audio(segment)
    if raw is None:
        return None
    return measure(storage, plan, segment, raw)


def _ranged(segment: PlannedSegment, stored: SegmentRange) -> PlannedSegment:
    return replace(
        segment,
        sample_rate=stored.sample_rate,
        frame_count=stored.frame_count,
        trim_start=stored.trim_start,
    )


def trimmed_audio(
    storage: NarrationStorage, plan: NarrationPlan, segment: PlannedSegment
) -> StoredAudio | None:
    """The Block's trimmed audio: the one read playback, Export, and Fill use."""
    raw = storage.raw_audio(segment)
    if raw is None:
        return None
    stored = storage.segment_range(plan.id, segment.ordinal, segment.key)
    if stored is None:
        measured = measure(storage, plan, segment, raw)
        return StoredAudio(
            raw.pcm[measured.trim_start : measured.trim_start + measured.frame_count],
            raw.sample_rate,
        )
    start, count = stored.trim_start, stored.frame_count
    return StoredAudio(raw.pcm[start : start + count], raw.sample_rate)


def recover_lengths(
    storage: NarrationStorage, plan: NarrationPlan, *, through: int | None = None
) -> NarrationPlan:
    """Measure every cached, unmeasured Block up to `through` (all when None).

    Shared and pre-migration audio arrives with no range on record; seek
    and Resume need the lengths ahead of the target to place it.
    """
    return replace(plan, segments=tuple(_recovered(storage, plan, through)))


def _recovered(
    storage: NarrationStorage, plan: NarrationPlan, through: int | None
) -> Iterable[PlannedSegment]:
    for segment in plan.segments:
        if (
            (through is None or segment.ordinal <= through)
            and not plan.is_gap(segment.ordinal)
            and not segment.measured
        ):
            measured = cached_block(storage, plan, segment)
            yield segment if measured is None else measured
        else:
            yield segment
