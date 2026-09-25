"""One Block-order audio walk for playback and Export."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fractions import Fraction

from readily_engine.audio import FloatPcm
from readily_engine.chunking import Boundary
from readily_engine.narration.assembly import Assembler
from readily_engine.narration.timeline import walk
from readily_engine.storage.segments import StoredAudio
from readily_engine.storage.storage import NarrationPlan, PlannedSegment


@dataclass(frozen=True)
class AssembledPiece:
    pcm: FloatPcm
    sample_rate: int
    end: Fraction
    pause_frames: int


def assemble(
    plan: NarrationPlan,
    resolve: Callable[[PlannedSegment], StoredAudio | None],
    *,
    first: int = 0,
    start: Fraction = Fraction(),
) -> Iterator[AssembledPiece]:
    """Resolve each Block once and render its trimmed audio or gap.

    Every piece covers the clock interval [position before, position after),
    and `start` is the playhead a Resume or a seek opens at: this is the one
    place the rule "play only what lies past the playhead" is applied. A
    piece ending at or before `start` is yielded with empty audio so the
    caller still sees the clock advance; the piece straddling `start` loses
    its leading frames. The resumed Block's own lead — the authored pause
    before it, or the predecessor tail it has no PCM for — falls inside that
    sliced region, so it needs no separate treatment.
    """
    assembler = Assembler(plan.settings.pause_policy, timeline=walk(plan, first))
    for segment in plan.segments[first:]:
        audio = resolve(segment)
        boundary = Boundary(segment.boundary)
        position = assembler.timeline.position
        pcm = (
            assembler.gap(boundary)
            if audio is None
            else assembler.add(audio.pcm, audio.sample_rate, boundary)
        )
        pause_frames = (
            round(assembler.last_lead_sec * audio.sample_rate)
            if audio is not None and assembler.last_lead_is_pause
            else 0
        )
        yield _piece(assembler, position, pcm, start, pause_frames)
    position = assembler.timeline.position
    yield _piece(assembler, position, assembler.flush(), start)


def _piece(
    assembler: Assembler,
    position: Fraction,
    pcm: FloatPcm,
    start: Fraction,
    pause_frames: int = 0,
) -> AssembledPiece:
    sample_rate = assembler.timeline.sample_rate
    end = assembler.timeline.position
    if position < start:
        skipped = round((start - position) * sample_rate)
        pcm = pcm[skipped:]
        pause_frames = max(0, pause_frames - skipped)
    return AssembledPiece(pcm, sample_rate, end, pause_frames)
