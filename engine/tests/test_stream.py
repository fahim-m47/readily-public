"""Resuming plays the same samples, from the playhead on.

`assemble` owns the whole resume rule, so a run opened at Block `first`
with a playhead has to be the tail of the run opened at zero, sample for
sample. Every seam class the playhead can land on gets a case: the
authored pause before a Block, the crossfade a mid-sentence cut holds, the
predecessor tail a too-short Block cannot absorb, a recorded gap, and a
playhead inside a pause rather than inside speech.
"""

from fractions import Fraction

import numpy as np
import pytest
from conftest import ENTRY
from generation_fakes import entry_for
from storage_fakes import SETTINGS, open_test_storage, publish_segment

from readily_engine.catalog import Tunables
from readily_engine.chunking import Block, Boundary, ChunkedSource
from readily_engine.narration.measure import trimmed_audio
from readily_engine.narration.stream import assemble
from readily_engine.narration.timeline import (
    CROSSFADE_SECONDS,
    locate_time,
    timeline_starts,
)
from readily_engine.storage.storage import NarrationPlan, NarrationStorage

KOKORO = Tunables.model_validate(ENTRY["tunables"])
RATE = 24_000
CROSSFADE_FRAMES = round(CROSSFADE_SECONDS * RATE)

MID, SENTENCE, PARAGRAPH = (
    Boundary.MID_SENTENCE,
    Boundary.SENTENCE,
    Boundary.PARAGRAPH,
)


def test_authored_pause_metadata_survives_a_seek_into_the_pause(tmp_path):
    storage = open_test_storage(tmp_path)
    plan = build(storage, (PARAGRAPH, SENTENCE), (RATE, RATE), ())
    pieces = list(assemble(plan, lambda part: trimmed_audio(storage, plan, part)))
    pause = pieces[1].pause_frames
    assert pause == round(
        plan.settings.pause_policy.pause_paragraph_break_ms * RATE / 1000
    )
    assert pieces[0].pause_frames == pieces[-1].pause_frames == 0
    start = pieces[0].end + Fraction(pause // 2, RATE)
    resumed = list(
        assemble(plan, lambda part: trimmed_audio(storage, plan, part), start=start)
    )
    assert resumed[1].pause_frames == pause - pause // 2
    np.testing.assert_array_equal(resumed[1].pcm, pieces[1].pcm[pause // 2 :])


def tone(seed: int, frames: int) -> np.ndarray:
    """Audible PCM: assembly trims silence, so zeros would be no Block."""
    generator = np.random.default_rng(seed)
    return np.clip(generator.standard_normal(frames) * 0.2, -0.6, 0.6).astype(
        np.float32
    )


def build(
    storage: NarrationStorage,
    boundaries: tuple[Boundary, ...],
    frames: tuple[int, ...],
    gaps: tuple[int, ...],
) -> NarrationPlan:
    blocks: list[Block] = []
    cursor = 0
    for index, boundary in enumerate(boundaries):
        text = f"Block {index} here."
        blocks.append(Block(text, cursor, cursor + len(text), boundary))
        cursor += len(text) + 1
    source = " ".join(block.text for block in blocks)
    plan = storage.create(
        ChunkedSource(source=source, blocks=tuple(blocks)),
        SETTINGS,
        entry_for(SETTINGS),
    )
    for segment, count in zip(plan.segments, frames, strict=True):
        if segment.ordinal in gaps:
            storage.record_gap(plan.id, segment, "generation_failed")
            continue
        publish_segment(storage, plan, segment, tone(segment.ordinal, count), RATE)
    return storage.plan(plan.id)


def walked(
    storage: NarrationStorage, plan: NarrationPlan, first: int, start: Fraction
) -> tuple[np.ndarray, Fraction]:
    """The PCM one `assemble` walk feeds, and the clock it ends on."""
    pieces = list(
        assemble(
            plan,
            lambda segment: trimmed_audio(storage, plan, segment),
            first=first,
            start=start,
        )
    )
    return np.concatenate([piece.pcm for piece in pieces]), pieces[-1].end


# Each case names the seam the playhead lands on, then the playhead itself
# as an offset in milliseconds from one Block's timeline start. `allowance`
# is how many opening frames a Resume may legitimately differ on: only the
# crossfade window, where the original mixed in a predecessor tail that a
# Resume has the length of but not the audio.
@pytest.mark.parametrize(
    "boundaries,frames,gaps,anchor,offset_ms,allowance",
    [
        ((PARAGRAPH, PARAGRAPH, SENTENCE), (2400, 2400, 2400), (), 1, 50, 0),
        ((PARAGRAPH, PARAGRAPH, SENTENCE), (2400, 2400, 2400), (), 1, 0, 0),
        ((MID, PARAGRAPH, SENTENCE), (2400, 2400, 2400), (), 1, 0, CROSSFADE_FRAMES),
        ((MID, PARAGRAPH, SENTENCE), (2400, 240, 2400), (), 1, 0, 0),
        ((PARAGRAPH, PARAGRAPH, SENTENCE), (2400, 2400, 2400), (1,), 2, 50, 0),
        ((PARAGRAPH, PARAGRAPH, SENTENCE), (2400, 2400, 2400), (), 1, -100, 0),
    ],
    ids=[
        "pause-lead",
        "on-the-seam",
        "blended-tail",
        "short-block-tail",
        "gap-before",
        "in-pause",
    ],
)
def test_a_resumed_walk_is_the_tail_of_the_whole_walk(
    tmp_path, boundaries, frames, gaps, anchor, offset_ms, allowance
):
    storage = open_test_storage(tmp_path)
    plan = build(storage, boundaries, frames, gaps)
    heard, total = walked(storage, plan, 0, Fraction())

    starts = timeline_starts(plan)
    assert starts[anchor] is not None
    playhead = Fraction(starts[anchor]) + Fraction(offset_ms, 1000)
    first = locate_time(plan, float(playhead), invalid=lambda part: False)
    resumed, end = walked(storage, plan, first, playhead)

    assert end == total
    expected = heard[round(playhead * RATE) :]
    assert len(resumed) == len(expected)
    assert np.array_equal(resumed[allowance:], expected[allowance:])


def test_a_playhead_past_the_end_feeds_nothing_but_still_reports_the_clock(tmp_path):
    storage = open_test_storage(tmp_path)
    plan = build(storage, (PARAGRAPH, SENTENCE), (2400, 2400), ())
    _heard, total = walked(storage, plan, 0, Fraction())

    resumed, end = walked(storage, plan, len(plan.segments) - 1, total)

    assert len(resumed) == 0
    assert end == total
