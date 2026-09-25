"""Decide where one Block's words come from, and place them on the timeline."""

from collections.abc import Iterable
from typing import Literal

from readily_engine.alignment import Aligner
from readily_engine.audio import FloatPcm
from readily_engine.catalog import CatalogEntry
from readily_engine.generation import GenerationRecord
from readily_engine.narration.measure import trim_range
from readily_engine.storage.storage import (
    NarrationPlan,
    NarrationStorage,
    PlannedSegment,
)
from readily_engine.timings import SourceTiming, Timing, place_timings

Route = Literal["spoken", "matched", "estimated"]


def word_route(entry: CatalogEntry, record: GenerationRecord) -> Route:
    """Which lane supplies a Block's words, fixed by its Generation Record."""
    if entry.reports_word_times(entry.voice(record.voice_id).language):
        return "spoken"
    return "estimated" if record.word_timing == "off" else "matched"


def block_timings(
    route: Route,
    generated: Iterable[Timing],
    *,
    aligner: Aligner | None,
    record: GenerationRecord,
    plan: NarrationPlan,
    segment: PlannedSegment,
    pcm: FloatPcm,
    sample_rate: int,
) -> tuple[Timing, ...]:
    """The words to store with a Block: synthesis's own on the spoken lane,
    the aligner's on the matched lane when synthesis gave none, and nothing
    otherwise so the seek path estimates. Aligned words are offset back
    into the untrimmed Segment."""
    generated = tuple(generated)
    if route == "spoken":
        return generated
    if generated or route != "matched" or aligner is None:
        return tuple(word for word in generated if word.provenance != "spoken")
    start, end = trim_range(plan, segment, pcm, sample_rate)
    offset = start / sample_rate
    return tuple(
        word.model_copy(
            update={
                "start_sec": word.start_sec + offset,
                "end_sec": word.end_sec + offset,
            }
        )
        for word in aligner.align(record.text, pcm[start:end], sample_rate)
    )


def words_for(
    storage: NarrationStorage,
    plan: NarrationPlan,
    segment: PlannedSegment,
    start_sec: float | None,
) -> tuple[SourceTiming, ...]:
    """The same word positions for seek and read-along, once audio is measured."""
    if start_sec is None or not segment.measured or plan.is_gap(segment.ordinal):
        return ()
    source = plan.source[segment.source_start : segment.source_end]
    start = segment.source_start + len(source) - len(source.lstrip())
    return place_timings(
        storage.timings(segment),
        source_start=start,
        text=segment.text,
        trim_start_sec=segment.trim_start / segment.sample_rate,
        duration_sec=segment.frame_count / segment.sample_rate,
        timeline_start_sec=start_sec,
    )
