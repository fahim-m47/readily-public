"""Assembly makes every seam deliberate: trim, pause, butt-join, crossfade."""

import numpy as np
import pytest
from conftest import ENTRY

from readily_engine.catalog import PausePolicy, Tunables
from readily_engine.chunking import Boundary
from readily_engine.narration.assembly import (
    EDGE_FADE_SECONDS,
    Assembler,
    trim_margins,
    trim_silence,
)
from readily_engine.narration.timeline import CROSSFADE_SECONDS

KOKORO = Tunables.model_validate(ENTRY["tunables"])
RATE = 24_000
FADE = round(EDGE_FADE_SECONDS * RATE)


def speech(seconds: float, value: float = 0.5) -> np.ndarray:
    return np.full(round(seconds * RATE), value, dtype=np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(round(seconds * RATE), dtype=np.float32)


def trim(pcm: np.ndarray, margin_ms: int = 40) -> np.ndarray:
    return trim_silence(pcm, RATE, lead_margin_ms=margin_ms, tail_margin_ms=margin_ms)


class RawFeed:
    """Feeds an `Assembler` the raw, model-padded audio these tests are
    written against. The Assembler takes only trimmed Segments, so the
    margin rule curation applies before every `add` lives here too."""

    def __init__(self, policy: PausePolicy, *, after: Boundary | None = None) -> None:
        self.policy = policy
        self.previous = after
        self.assembler = Assembler(policy)

    def add(self, pcm: np.ndarray, sample_rate: int, boundary: Boundary) -> np.ndarray:
        lead, tail = trim_margins(self.policy, self.previous, boundary)
        self.previous = boundary
        trimmed = trim_silence(
            pcm, sample_rate, lead_margin_ms=lead, tail_margin_ms=tail
        )
        return self.assembler.add(trimmed, sample_rate, boundary)

    def gap(self, boundary: Boundary) -> np.ndarray:
        self.previous = boundary
        return self.assembler.gap(boundary)

    def flush(self) -> np.ndarray:
        return self.assembler.flush()

    @property
    def last_lead_sec(self) -> float:
        return self.assembler.last_lead_sec

    @property
    def last_lead_is_pause(self) -> bool:
        return self.assembler.last_lead_is_pause


def test_trim_keeps_forty_ms_of_quiet_release_and_onset_on_both_sides():
    padded = np.concatenate([silence(0.1), speech(0.2), silence(0.3)])

    trimmed = trim(padded)

    np.testing.assert_array_equal(
        trimmed, np.concatenate([silence(0.04), speech(0.2), silence(0.04)])
    )


def test_trim_margin_stops_at_the_segment_edges():
    pcm = np.concatenate([silence(0.02), speech(0.2), silence(0.01)])
    np.testing.assert_array_equal(trim(pcm), pcm)


def test_a_saved_version_one_policy_keeps_the_original_trim_and_timeline():
    assembler = RawFeed(KOKORO.model_copy(update={"pause_policy_version": 1}))
    padded = np.concatenate([silence(0.1), speech(0.2), silence(0.3)])
    first = assembler.add(padded, RATE, Boundary.SENTENCE)
    second = assembler.add(padded, RATE, Boundary.PARAGRAPH)
    pause = round(KOKORO.pause_sentence_ms / 1000 * RATE)
    assert len(first) == round(0.2 * RATE)
    assert len(second) == pause + round(0.2 * RATE)
    assert first[FADE] == 0.5
    assert second[pause + FADE] == 0.5


def test_trim_keeps_the_subframe_tail_when_speech_runs_to_the_end():
    pcm = np.concatenate([speech(0.2), speech(0.003)])

    assert len(trim(pcm)) == len(pcm)


def test_trim_of_pure_silence_is_empty():
    assert len(trim(silence(0.5))) == 0


def test_a_sentence_seam_is_the_tunables_short_pause():
    assembler = RawFeed(KOKORO)

    first = assembler.add(speech(0.2), RATE, Boundary.SENTENCE)
    second = assembler.add(speech(0.2), RATE, Boundary.PARAGRAPH)

    pause = round(KOKORO.pause_sentence_ms / 1000 * RATE)
    assert np.all(second[:pause] == 0)
    assert second[pause + FADE] == 0.5
    assert len(first) + len(second) == round(0.4 * RATE) + pause


def test_a_paragraph_seam_is_the_tunables_long_pause():
    assembler = RawFeed(KOKORO)

    assembler.add(speech(0.2), RATE, Boundary.PARAGRAPH)
    second = assembler.add(speech(0.2), RATE, Boundary.PARAGRAPH)

    pause = round(KOKORO.pause_paragraph_break_ms / 1000 * RATE)
    assert np.all(second[:pause] == 0)
    assert second[pause + FADE] == 0.5


def test_the_models_padding_is_bounded_by_the_trim_margin():
    assembler = RawFeed(KOKORO)

    assembler.add(np.concatenate([speech(0.2), silence(0.4)]), RATE, Boundary.SENTENCE)
    second = assembler.add(
        np.concatenate([silence(0.4), speech(0.2)]), RATE, Boundary.PARAGRAPH
    )

    pause = round(KOKORO.pause_sentence_ms / 1000 * RATE)
    assert len(second) == pause + round(0.24 * RATE)


def test_a_mid_sentence_seam_is_a_sample_accurate_crossfade_not_a_pause():
    assembler = RawFeed(KOKORO)

    first = assembler.add(speech(0.2), RATE, Boundary.MID_SENTENCE)
    second = assembler.add(speech(0.2, value=-0.5), RATE, Boundary.PARAGRAPH)

    fade = round(CROSSFADE_SECONDS * RATE)
    assert len(first) == round(0.2 * RATE) - fade
    # No authored silence anywhere in the seam, and the blend moves
    # smoothly between the two sides' levels.
    assert np.all(second[fade:-FADE] == -0.5)
    blend = second[:fade]
    assert blend[0] == pytest.approx(0.5, abs=0.01)
    assert blend[-1] == pytest.approx(-0.5, abs=0.01)
    assert len(first) + len(second) == round(0.4 * RATE) - fade


def test_a_mid_sentence_seam_keeps_no_trim_margin_on_either_side():
    """The version 2 margin protects releases before authored pauses. At a
    crossfade it would blend quiet into quiet with a gap on both sides."""
    assembler = RawFeed(KOKORO)

    first = assembler.add(
        np.concatenate([silence(0.1), speech(0.2), silence(0.3)]),
        RATE,
        Boundary.MID_SENTENCE,
    )
    second = assembler.add(
        np.concatenate([silence(0.3), speech(0.2, value=-0.5), silence(0.1)]),
        RATE,
        Boundary.PARAGRAPH,
    )

    fade = round(CROSSFADE_SECONDS * RATE)
    margin = round(0.04 * RATE)
    assert len(first) == margin + round(0.2 * RATE) - fade
    assert first[-1] == 0.5
    assert np.all(second[:fade] != 0)
    assert np.all(second[fade : -(margin + FADE)] == -0.5)
    assert len(second) == round(0.2 * RATE) + margin


def test_a_resume_after_a_mid_sentence_split_trims_like_the_original_run():
    """The original run trimmed this Block's opening with no margin because
    it blended into the previous tail. A Resume has no tail, but must still
    open on the same sample or the recorded offset lands a margin late."""
    padded = np.concatenate([silence(0.1), speech(0.2), silence(0.1)])
    original = RawFeed(KOKORO)
    original.add(padded, RATE, Boundary.MID_SENTENCE)
    live = original.add(padded, RATE, Boundary.PARAGRAPH)

    resumed = RawFeed(KOKORO, after=Boundary.MID_SENTENCE).add(
        padded, RATE, Boundary.PARAGRAPH
    )

    fade = round(CROSSFADE_SECONDS * RATE)
    assert len(resumed) == len(live)
    np.testing.assert_array_equal(resumed[fade:], live[fade:])


def test_an_exposed_segment_edge_ramps_instead_of_stepping():
    """`trim_silence` cuts on frame boundaries, so a Segment can open and
    close part-way up a waveform. Played against silence that is a step, and
    the step is the click qualification check B refuses."""
    assembler = RawFeed(KOKORO)

    out = assembler.add(speech(0.2), RATE, Boundary.PARAGRAPH)

    assert out[0] == 0
    assert out[-1] == 0
    assert np.max(np.abs(np.diff(out))) == pytest.approx(0.5 / (FADE - 1))
    assert np.all(out[FADE:-FADE] == 0.5)


def test_an_opening_the_previous_segment_runs_into_is_left_alone():
    """A blend and a butt-join both open on audio that is already playing.
    Ramping there would dip a seam that is continuous by construction."""
    assembler = RawFeed(KOKORO)

    assembler.add(speech(0.2), RATE, Boundary.MID_SENTENCE)
    # One 10ms trim frame: too short to absorb the blend, so the held tail
    # plays out plain and this Segment butt-joins onto it.
    second = assembler.add(speech(0.010, value=-0.5), RATE, Boundary.PARAGRAPH)

    tail = round(CROSSFADE_SECONDS * RATE)
    assert np.all(second[:tail] == 0.5)
    assert second[tail] == -0.5


def test_the_final_segment_ends_without_authored_silence():
    assembler = RawFeed(KOKORO)

    out = assembler.add(speech(0.2), RATE, Boundary.PARAGRAPH)
    tail = assembler.flush()

    assert len(out) == round(0.2 * RATE)
    assert len(tail) == 0


def test_flush_releases_a_held_crossfade_tail():
    assembler = RawFeed(KOKORO)

    out = assembler.add(speech(0.2), RATE, Boundary.MID_SENTENCE)
    tail = assembler.flush()

    fade = round(CROSSFADE_SECONDS * RATE)
    assert len(tail) == fade
    assert len(out) + len(tail) == round(0.2 * RATE)
    assert len(assembler.flush()) == 0


def test_a_gap_resolves_the_seam_as_a_break_not_a_splice():
    assembler = RawFeed(KOKORO)

    assembler.add(speech(0.2), RATE, Boundary.SENTENCE)
    tail = assembler.gap(Boundary.PARAGRAPH)
    after = assembler.add(speech(0.2), RATE, Boundary.PARAGRAPH)

    # The failed Block owed the paragraph pause; the seam around the gap
    # carries it, so missing content is audible as a break.
    pause = round(KOKORO.pause_paragraph_break_ms / 1000 * RATE)
    assert len(tail) == 0
    assert np.all(after[:pause] == 0)
    assert after[pause + FADE] == 0.5


def test_a_gap_after_a_waterfall_cut_plays_the_held_tail_plain():
    assembler = RawFeed(KOKORO)

    assembler.add(speech(0.2), RATE, Boundary.MID_SENTENCE)
    tail = assembler.gap(Boundary.SENTENCE)

    fade = round(CROSSFADE_SECONDS * RATE)
    assert len(tail) == fade
    # Plain playout, apart from the ramp down into the break that follows it.
    assert np.all(tail[:-FADE] == 0.5)
    assert tail[-1] == 0


def test_a_sample_rate_change_degrades_the_crossfade_to_a_butt_join():
    assembler = RawFeed(KOKORO)

    first = assembler.add(speech(0.2), RATE, Boundary.MID_SENTENCE)
    other_rate = 48_000
    second = assembler.add(
        np.full(round(0.2 * other_rate), -0.5, dtype=np.float32),
        other_rate,
        Boundary.PARAGRAPH,
    )

    fade = round(EDGE_FADE_SECONDS * other_rate)
    assert np.all(second[fade:-fade] == -0.5)
    # The one seam the fade cannot reach, asserted rather than believed:
    # `first` was already handed to the caller ending on speech, and the tail
    # it held back for the next Segment to blend with is at the old rate and
    # gets dropped, so the next piece ramps up from zero against it.
    assert len(first) == round(0.2 * RATE) - round(CROSSFADE_SECONDS * RATE)
    assert first[-1] == 0.5
    assert second[0] == 0


@pytest.mark.parametrize(
    ("first_boundary", "second_seconds", "has_lead", "lead_is_pause"),
    [
        (Boundary.SENTENCE, 0.2, True, True),
        (Boundary.MID_SENTENCE, 0.2, False, False),
        # One 10ms trim frame: short enough that the 15ms fade window
        # cannot fit, long enough to survive the trim as real speech.
        (Boundary.MID_SENTENCE, 0.010, True, False),
    ],
    ids=["authored-pause", "blend", "tail-playout"],
)
def test_a_lead_says_whether_it_is_silence_or_the_previous_block_speaking(
    first_boundary, second_seconds, has_lead, lead_is_pause
):
    """Curation cuts the stream into speech and the pause after it, and only
    the Assembler knows which kind of lead it just emitted. A Segment too
    short to absorb a blend gets the held tail played out plain — the
    previous Block's speech, arriving at the head of this Block's piece and
    belonging to neither the blend case nor the pause case."""
    assembler = RawFeed(KOKORO)

    assembler.add(speech(0.2), RATE, first_boundary)
    assembler.add(speech(second_seconds, value=-0.5), RATE, Boundary.PARAGRAPH)

    assert bool(assembler.last_lead_sec) is has_lead
    assert assembler.last_lead_is_pause is lead_is_pause


@pytest.mark.parametrize(
    "first_boundary",
    [Boundary.MID_SENTENCE, Boundary.SENTENCE, Boundary.PARAGRAPH],
    ids=["blend", "short-pause", "long-pause"],
)
def test_a_resumed_block_lands_where_its_recorded_offset_says(first_boundary):
    """`last_lead_sec` has to name where the Block's own speech begins.

    Curation splits each piece at that offset to judge the Block's audio
    apart from the seam in front of it. So the offset has to point at where
    the speech began, not where the piece began: a Block rendered alone —
    with no seam behind it to resolve — must land exactly there inside the
    run it was recorded from, or every measurement is off by a seam.
    """
    original = RawFeed(KOKORO)
    cursor = 0.0

    first = original.add(speech(0.2), RATE, first_boundary)
    cursor += len(first) / RATE

    # What the worker stores for the second Block: read after its own
    # `add`, from the cursor the piece starts on.
    second = original.add(speech(0.2, value=-0.5), RATE, Boundary.PARAGRAPH)
    anchor = cursor + original.last_lead_sec
    timeline = np.concatenate([first, second, original.flush()])

    resumed = RawFeed(KOKORO)
    replayed = np.concatenate(
        [
            resumed.add(speech(0.2, value=-0.5), RATE, Boundary.PARAGRAPH),
            resumed.flush(),
        ]
    )

    # The blended window is the one place the two runs may legitimately
    # differ: the original mixed this Block's opening into the previous
    # Block's tail, and a Resume has no tail to mix with. Everything past
    # it has to line up sample for sample.
    skip = (
        round(CROSSFADE_SECONDS * RATE)
        if first_boundary is Boundary.MID_SENTENCE
        else 0
    )
    start = round(anchor * RATE)
    # An anchor past the seam would push the Block off the end of the run
    # it was recorded from.
    assert start + len(replayed) <= len(timeline)
    assert np.allclose(timeline[start + skip : start + len(replayed)], replayed[skip:])


def played(assembler: RawFeed, segments) -> np.ndarray:
    """Every sample the reader hears, in the order they hear it — the pieces
    `add` returns and whatever `flush` was still holding, end to end. Seams
    live *between* pieces, so a test that inspects one piece at a time is
    looking everywhere except at the thing that clicks."""
    pieces = [assembler.add(pcm, RATE, boundary) for pcm, boundary in segments]
    pieces.append(assembler.flush())
    return np.concatenate([piece for piece in pieces if len(piece)])


# The line between a ramped edge and an exposed one, in per-sample step
# size. Across a stream held at one steady level every seam is either a
# ramp or sample-continuous, so the biggest jump is one increment of the
# ramp — 0.5/71 here, or a little more where the equal-power blend has
# lifted the level it crosses towards 0.707. An edge left exposed steps by
# the level itself, twenty times further, and that is the click.
MAX_RAMPED_STEP = 0.025


def test_a_segment_that_trims_to_nothing_leaves_no_step_behind_the_held_tail():
    # The reachable one: a Block whose duration predictor collapses comes back
    # as padding and trims away, so the piece that plays is the *previous*
    # Segment's held tail — which the crossfade was going to carry, and now
    # nothing does. It ends on speech, and a paragraph pause follows it.
    assembler = RawFeed(KOKORO)

    out = played(
        assembler,
        [
            (speech(0.2), Boundary.MID_SENTENCE),
            (silence(0.2), Boundary.PARAGRAPH),
            (speech(0.2), Boundary.SENTENCE),
        ],
    )

    assert float(np.max(np.abs(np.diff(out)))) < MAX_RAMPED_STEP


def test_a_segment_the_blend_swallows_whole_leaves_no_step_behind_the_blend():
    # A Segment exactly as long as the crossfade window is consumed entirely
    # by the blend that opens it, so the piece ends on the blended region and
    # there is no remainder for a fade applied to the remainder to reach.
    assembler = RawFeed(KOKORO)

    out = played(
        assembler,
        [
            (speech(0.2), Boundary.MID_SENTENCE),
            (speech(round(CROSSFADE_SECONDS * RATE) / RATE), Boundary.PARAGRAPH),
            (speech(0.2), Boundary.SENTENCE),
        ],
    )

    assert float(np.max(np.abs(np.diff(out)))) < MAX_RAMPED_STEP


def test_an_ordinary_paragraph_seam_leaves_no_step_either():
    # The baseline the two above are measured against: same assertion, same
    # steady level, no held tail involved anywhere.
    assembler = RawFeed(KOKORO)

    out = played(
        assembler,
        [
            (speech(0.2), Boundary.PARAGRAPH),
            (speech(0.2), Boundary.SENTENCE),
        ],
    )

    assert float(np.max(np.abs(np.diff(out)))) < MAX_RAMPED_STEP
