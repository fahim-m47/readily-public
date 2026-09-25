"""Sample-accurate seam decisions shared by History and audio assembly."""

from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

from readily_engine.catalog import PausePolicy
from readily_engine.chunking import Boundary
from readily_engine.storage.storage import NarrationPlan, PlannedSegment

CROSSFADE_SECONDS = 0.015


@dataclass(frozen=True)
class Seam:
    start: Fraction
    end: Fraction
    lead_frames: int
    blend_frames: int
    hold_frames: int
    lead_is_pause: bool
    opens_on_silence: bool


class TimelineWalker:
    """Walk trimmed lengths without reading PCM or recording positions.

    The Assembler executes these same seam decisions on PCM. Frame counts stay
    integral; Fraction carries the clock across the rare sample-rate change.
    """

    def __init__(self, policy: PausePolicy) -> None:
        self.position = Fraction()
        self.sample_rate = 0
        self.held_frames = 0
        self.pending_pause_ms = 0
        self._pauses = {
            Boundary.SENTENCE: policy.pause_sentence_ms,
            Boundary.PARAGRAPH: policy.pause_paragraph_break_ms,
            Boundary.MID_SENTENCE: 0,
        }

    def add(self, frames: int, sample_rate: int, boundary: Boundary) -> Seam:
        if sample_rate != self.sample_rate:
            self.held_frames = 0
            self.sample_rate = sample_rate
        tail = self.held_frames
        blend = tail if tail and frames >= tail else 0
        lead_is_pause = not tail and self.pending_pause_ms > 0 and frames > 0
        lead = tail if tail and not blend else 0
        if lead_is_pause:
            lead = round(Fraction(self.pending_pause_ms * sample_rate, 1000))
            self.pending_pause_ms = 0
        remaining = frames - blend
        crossfade = round(CROSSFADE_SECONDS * sample_rate)
        hold = (
            crossfade
            if boundary is Boundary.MID_SENTENCE and remaining > crossfade
            else 0
        )
        self.held_frames = hold
        if hold:
            self.pending_pause_ms = 0
        else:
            self.pending_pause_ms = max(self.pending_pause_ms, self._pauses[boundary])
        start = self.position + Fraction(lead, sample_rate)
        self.position += Fraction(lead + frames - hold, sample_rate)
        return Seam(start, self.position, lead, blend, hold, lead_is_pause, not tail)

    def advance(self, segment: PlannedSegment, *, gap: bool) -> Fraction | None:
        """Advance from stored lengths, or stop at an unknown Block."""
        boundary = Boundary(segment.boundary)
        if segment.frame_count is not None and segment.sample_rate is not None:
            return self.add(segment.frame_count, segment.sample_rate, boundary).start
        if gap:
            self.gap(boundary)
            return self.position
        return None

    def gap(self, boundary: Boundary) -> int:
        self.pending_pause_ms = max(self.pending_pause_ms, self._pauses[boundary])
        return self._release_tail()

    def flush(self) -> int:
        self.pending_pause_ms = 0
        return self._release_tail()

    def _release_tail(self) -> int:
        frames = self.held_frames
        self.held_frames = 0
        if self.sample_rate:
            self.position += Fraction(frames, self.sample_rate)
        return frames


def walk(plan: NarrationPlan, count: int) -> TimelineWalker:
    """A walker advanced through the plan's first `count` Blocks.

    The seam of Block `count` depends on what preceded it — a held tail, a
    pending pause, the clock — so anything starting mid-Narration replays
    the prefix rather than starting from zero.
    """
    walker = TimelineWalker(plan.settings.pause_policy)
    for segment in plan.segments[:count]:
        walker.advance(segment, gap=plan.is_gap(segment.ordinal))
    return walker


def known_length(plan: NarrationPlan) -> float:
    """Seconds of the Narration already assembled: its measured prefix.

    A job that starts mid-Narration walks the plan again from its target, so
    on its own it would report a length that begins over at the seek and
    grows back Block by Block. This is the floor under that report.
    """
    walker = TimelineWalker(plan.settings.pause_policy)
    for segment in plan.segments:
        if walker.advance(segment, gap=plan.is_gap(segment.ordinal)) is None:
            break
    return float(walker.position)


def timeline_starts(plan: NarrationPlan) -> tuple[float | None, ...]:
    """Derive the known prefix; unknown lengths cannot promise later offsets."""
    walker = TimelineWalker(plan.settings.pause_policy)
    starts: list[float | None] = []
    for segment in plan.segments:
        start = walker.advance(segment, gap=plan.is_gap(segment.ordinal))
        if start is None:
            break
        starts.append(float(start))
    return tuple(starts + [None] * (len(plan.segments) - len(starts)))


def locate_time(
    plan: NarrationPlan,
    position_sec: float,
    *,
    invalid: Callable[[PlannedSegment], bool],
) -> int:
    """Open Resume at the last known Block before its saved playhead."""
    walker = TimelineWalker(plan.settings.pause_policy)
    first = 0
    for index, segment in enumerate(plan.segments):
        if invalid(segment):
            break
        offset = walker.advance(segment, gap=plan.is_gap(segment.ordinal))
        if offset is None:
            break
        if float(offset) > position_sec:
            break
        first = index
    return first


def block_at(plan: NarrationPlan, source_offset: int) -> PlannedSegment | None:
    """The Block spoken at a Source offset; None outside the Source."""
    if not 0 <= source_offset < len(plan.source):
        return None
    return next(
        (part for part in plan.segments if part.source_end > source_offset), None
    )


def locate_source(
    plan: NarrationPlan,
    segment: PlannedSegment,
    *,
    invalid: Callable[[PlannedSegment], bool],
) -> Fraction | None:
    """The timeline start of `segment`, or None when it cannot be placed.

    An earlier Block of unknown length, or a retired Block up to and
    including the target, leaves it unplaceable. The first Block is the
    exception: it opens the timeline whatever its own state, since the
    worker refreshes it in place.
    """
    walker = TimelineWalker(plan.settings.pause_policy)
    parts = plan.segments[: segment.ordinal + 1]
    if segment.ordinal and any(invalid(part) for part in parts):
        return None
    for part in parts[:-1]:
        if walker.advance(part, gap=plan.is_gap(part.ordinal)) is None:
            return None
    start = walker.advance(segment, gap=plan.is_gap(segment.ordinal))
    if start is None:
        return Fraction() if segment.ordinal == 0 else None
    return start
