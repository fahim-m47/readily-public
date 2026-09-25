"""The chunker may lose a Block's worth of prosody; it may never lose a word.

Silent text loss is the one forbidden outcome (ADR 0002 §8), and it is the
kind of bug nobody reports: the narration just... skips a sentence, and the
listener assumes they missed it. So the headline test here is a property over
arbitrary input — the Blocks always recombine to the normalized Source — and
the rest pin the policy ADR 0002 §2 fixed: paragraph-respecting greedy
packing, a short first Block, and a punctuation waterfall that cuts an
oversized sentence rather than truncating it.
"""

from itertools import pairwise

import pysbd
import pytest
from conftest import ENTRY
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from readily_engine.catalog import Tunables, load_manifest
from readily_engine.chunking import (
    _WINDOW_CHARS,
    Block,
    Boundary,
    ChunkedSource,
    _sentences,
    _windows,
    chunk,
    normalize,
)

# The committed kokoro-class budgets: 450-character Blocks, a 150-character
# first Block. Read off the shared entry so a curation change reaches here.
KOKORO = Tunables.model_validate(ENTRY["tunables"])

# pysbd walks a pile of regexes per paragraph, so the property tests get a
# deadline they cannot trip on a loaded CI runner rather than a fast one.
PROPERTY = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# Text shaped like something a person would paste: words, sentence
# punctuation, soft wraps and blank lines. `st.text()` covers the Unicode
# cruft; this covers the structure the chunker actually cuts on.
PROSE = st.lists(
    st.tuples(
        st.text(
            st.characters(codec="ascii", exclude_categories=("C", "Z")),
            min_size=1,
            max_size=12,
        ),
        st.sampled_from(
            [" ", " ", " ", ". ", "! ", "? ", ", ", "; ", ": ", "\n", "\n\n", "—"]
        ),
    ),
    max_size=120,
).map(lambda pairs: "".join(word + separator for word, separator in pairs))


def blocks(source: str, tunables: Tunables = KOKORO) -> list[Block]:
    return list(chunk(source, tunables).blocks)


def spoken_texts(source: str, tunables: Tunables = KOKORO) -> list[str]:
    """The text each Block hands to synthesis, without its Source separator."""
    return [block.text.strip() for block in blocks(source, tunables)]


def assert_nothing_is_lost(chunked: ChunkedSource) -> None:
    """Blocks tile the normalized Source exactly, without gaps or overlaps."""
    cursor = 0
    for block in chunked.blocks:
        assert block.text, "a Block is never empty"
        assert block.start == cursor, "Blocks run in order without gaps or overlaps"
        assert chunked.source[block.start : block.end] == block.text
        cursor = block.end
    assert cursor == len(chunked.source)
    assert "".join(block.text for block in chunked.blocks) == chunked.source


@PROPERTY
@given(st.text(max_size=800))
def test_arbitrary_text_recombines_to_the_normalized_source(source):
    assert_nothing_is_lost(chunk(source, KOKORO))


@PROPERTY
@given(PROSE)
def test_prose_recombines_to_the_normalized_source(source):
    assert_nothing_is_lost(chunk(source, KOKORO))


@PROPERTY
@given(PROSE, st.integers(min_value=1, max_value=200))
def test_no_budget_is_too_small_to_chunk_safely(source, budget):
    # The budgets in the Catalog are hundreds of characters, but a budget
    # small enough to bisect words is where an off-by-one in the waterfall
    # would drop one — so the property has to hold down there too.
    tunables = KOKORO.model_copy(
        update={"chunk_budget_chars": budget, "first_block_chars": budget}
    )
    assert_nothing_is_lost(chunk(source, tunables))


@PROPERTY
@given(PROSE)
def test_every_block_fits_the_budget(source):
    chunked = chunk(source, KOKORO)
    assert all(len(block.text) <= KOKORO.chunk_budget_chars for block in chunked.blocks)
    if chunked.blocks:
        assert len(chunked.blocks[0].text) <= KOKORO.first_block_chars


def long_paragraph(sentence: str, windows: int) -> str:
    """One paragraph of repeated `sentence`, spanning `windows` pysbd windows."""
    paragraph = sentence
    while len(paragraph) < windows * _WINDOW_CHARS:
        paragraph += " " + sentence
    return paragraph


def assert_no_block_seam_splits_a_word(chunked: ChunkedSource) -> None:
    for block in chunked.blocks[:-1]:
        seam = chunked.source[block.end - 1 : block.end + 1]
        assert not seam.isalnum(), f"a Block seam inside a word at {block.end}"


def test_a_paragraph_shorter_than_a_window_is_segmented_as_pysbd_says():
    source = long_paragraph("Dr. Who met Mr. Smith at 3.5 p.m. and said hello.", 1)[
        : _WINDOW_CHARS // 2
    ]
    segmenter = pysbd.Segmenter(language="en", clean=False)

    assert [source[s.start : s.end].strip() for s in _sentences(source)] == [
        sentence.strip() for sentence in segmenter.segment(source)
    ]


def test_a_long_single_paragraph_tiles_the_source_across_window_seams():
    chunked = chunk(long_paragraph("Sentence number one says something.", 6), KOKORO)

    assert len(chunked.source) > 5 * _WINDOW_CHARS
    assert_nothing_is_lost(chunked)
    assert_no_block_seam_splits_a_word(chunked)
    assert all(block.boundary is not Boundary.MID_SENTENCE for block in chunked.blocks)


def test_a_window_seam_after_an_abbreviation_is_not_a_sentence_pause():
    paragraph = long_paragraph("Dr. Who met Mr. Smith and said a word or two.", 4)

    windows = _windows(paragraph)
    chunked = chunk(paragraph, KOKORO)

    assert len(windows) > 1
    assert windows[0][0] == 0 and windows[-1][1] == len(paragraph)
    for (_, end), (next_start, _) in pairwise(windows):
        assert end == next_start
        assert paragraph[end - 1] == "." and paragraph[end] == " "
    seams = {end for _, end in windows[:-1]}
    assert any(paragraph[end - 3 : end] in ("Dr.", "Mr.") for end in seams)
    assert_nothing_is_lost(chunked)
    for block in chunked.blocks:
        if block.end in seams:
            expected = (
                Boundary.MID_SENTENCE
                if block.text.endswith(("Dr.", "Mr."))
                else Boundary.SENTENCE
            )
            assert block.boundary is expected, block.text[-20:]


def test_a_window_with_no_sentence_end_seams_at_a_space_without_a_pause():
    chunked = chunk(long_paragraph("word " * 40 + "word,", 3), KOKORO)

    assert_nothing_is_lost(chunked)
    assert_no_block_seam_splits_a_word(chunked)
    assert [block.boundary for block in chunked.blocks][:-1] == [
        Boundary.MID_SENTENCE
    ] * (len(chunked.blocks) - 1)
    assert chunked.blocks[-1].boundary is Boundary.PARAGRAPH


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(PROSE)
def test_a_paragraph_spanning_windows_recombines_to_the_normalized_source(source):
    paragraph = long_paragraph(source.replace("\n", " ").strip() or "x", 2)

    assert_nothing_is_lost(chunk(paragraph, KOKORO))


@PROPERTY
@given(st.text(max_size=800))
def test_normalizing_a_normalized_source_changes_nothing(source):
    once = normalize(source)
    assert normalize(once) == once


@pytest.mark.parametrize("entry", load_manifest().models, ids=lambda entry: entry.id)
def test_the_committed_entries_budgets_are_honoured(entry):
    # 450 for the kokoro-class instant Tier, ~300 for the expressive one:
    # per-entry because they are properties of the model, not the pipeline.
    source = " ".join(f"Sentence number {n} says something." for n in range(200))
    chunked = chunk(source, entry.tunables)

    assert [len(block.text) for block in chunked.blocks][:1] != []
    assert all(
        len(block.text) <= entry.tunables.chunk_budget_chars for block in chunked.blocks
    )
    assert len(chunked.blocks[0].text) <= entry.tunables.first_block_chars
    assert_nothing_is_lost(chunked)


def test_a_smaller_budget_makes_more_blocks():
    source = " ".join(f"Sentence number {n} says something." for n in range(200))
    chatterbox = KOKORO.model_copy(update={"chunk_budget_chars": 300})

    assert len(blocks(source, chatterbox)) > len(blocks(source, KOKORO))


def test_sentences_pack_greedily_up_to_the_budget():
    source = "One. Two. Three. Four. Five. Six."

    assert spoken_texts(source) == ["One.", "Two. Three. Four. Five. Six."]


def test_a_block_never_crosses_a_paragraph_boundary():
    source = "Alpha one. Alpha two.\n\nBeta one. Beta two."

    assert spoken_texts(source) == [
        "Alpha one.",
        "Alpha two.",
        "Beta one. Beta two.",
    ]


def test_soft_wrapped_lines_are_one_paragraph():
    # A PDF paste wraps at the column the PDF used. Honouring those newlines
    # would make one Block per printed line.
    source = "The first line wraps\nonto a second line.\n\nA new paragraph."

    assert spoken_texts(source) == [
        "The first line wraps onto a second line.",
        "A new paragraph.",
    ]


def test_a_source_with_no_text_has_no_blocks():
    assert blocks("") == []
    assert blocks("   \n\n\t  ") == []


def test_the_first_block_is_one_sentence():
    source = "Short. This second sentence would have fitted alongside it."

    assert spoken_texts(source)[0] == "Short."


def test_a_long_first_sentence_is_cut_to_the_first_block_budget():
    opening = "A first sentence that runs on " + "and on " * 40 + "before it ends."

    first, *rest = blocks(opening)

    assert len(first.text) <= KOKORO.first_block_chars
    assert first.boundary is Boundary.MID_SENTENCE
    # What is left of that sentence goes back on the full budget rather than
    # being minced into more 150-character Blocks.
    assert any(len(block.text) > KOKORO.first_block_chars for block in rest)


def test_a_two_hundred_word_sentence_splits_rather_than_truncating():
    sentence = (
        "This is a run-on sentence " + "that keeps going and going " * 60 + "and stops."
    )
    assert len(sentence.split()) > 200

    chunked = chunk(sentence, KOKORO)

    assert len(chunked.blocks) > 1
    assert all(len(block.text) <= KOKORO.chunk_budget_chars for block in chunked.blocks)
    assert_nothing_is_lost(chunked)


def test_the_waterfall_prefers_a_clause_break_to_a_comma():
    tunables = KOKORO.model_copy(
        update={"chunk_budget_chars": 40, "first_block_chars": 40}
    )
    source = "one, two; three, four five six seven eight nine ten."

    assert spoken_texts(source, tunables)[0] == "one, two;"


def test_the_waterfall_prefers_a_comma_to_a_bare_space():
    tunables = KOKORO.model_copy(
        update={"chunk_budget_chars": 30, "first_block_chars": 30}
    )
    source = "alpha beta, gamma delta epsilon zeta eta theta."

    assert spoken_texts(source, tunables)[0] == "alpha beta,"


def test_the_waterfall_does_not_cut_inside_a_decimal():
    tunables = KOKORO.model_copy(
        update={"chunk_budget_chars": 24, "first_block_chars": 24}
    )
    source = "the ratio was 3.14159 to one and it stayed there"

    assert spoken_texts(source, tunables)[0] == "the ratio was 3.14159"


def test_an_unbreakable_run_is_cut_at_the_budget_rather_than_dropped():
    # A 500-character URL has no punctuation and no whitespace to cut at.
    # Cutting at the budget is ugly; losing the tail is forbidden.
    tunables = KOKORO.model_copy(
        update={"chunk_budget_chars": 100, "first_block_chars": 100}
    )
    source = "x" * 250

    chunked = chunk(source, tunables)

    assert [len(block.text) for block in chunked.blocks] == [100, 100, 50]
    assert_nothing_is_lost(chunked)


def test_a_block_reports_how_it_ends():
    source = "One. Two.\n\nThree."

    assert [block.boundary for block in blocks(source)] == [
        Boundary.SENTENCE,
        Boundary.PARAGRAPH,
        Boundary.PARAGRAPH,
    ]


def test_a_waterfall_cut_is_the_only_mid_sentence_boundary():
    tunables = KOKORO.model_copy(
        update={"chunk_budget_chars": 20, "first_block_chars": 20}
    )
    source = "alpha beta gamma delta epsilon zeta."

    boundaries = [block.boundary for block in blocks(source, tunables)]

    assert boundaries[-1] is Boundary.PARAGRAPH
    assert set(boundaries[:-1]) == {Boundary.MID_SENTENCE}


def test_line_endings_and_separators_become_newlines():
    assert normalize("a\r\nb\rc\u2028d") == "a b c d"
    assert normalize("a\u2029b") == "a\n\nb"


def test_blank_lines_are_paragraph_breaks_however_many_there_are():
    assert normalize("a\n\n\n\n\nb") == "a\n\nb"
    assert normalize("a\n   \n b") == "a\n\nb"


def test_runs_of_whitespace_collapse_to_one_space():
    assert normalize("a\tb") == "a b"
    assert normalize("a \t\u00a0 b") == "a b"


def test_invisible_characters_are_dropped_rather_than_phonemized():
    # A BOM, a zero-width space, a bidi mark and the soft hyphen a PDF
    # extraction leaves mid-word.
    assert normalize("\ufeffa\u200bb\u200ec\u00add") == "abcd"


def test_normalization_folds_to_nfc():
    assert normalize("e\u0301") == "\u00e9"


def test_block_ranges_index_the_normalized_source_not_the_raw_one():
    chunked = chunk("  Padded.\r\n\r\nSecond.  ", KOKORO)

    assert chunked.source == "Padded.\n\nSecond."
    assert [(block.start, block.end) for block in chunked.blocks] == [(0, 7), (7, 16)]
    assert_nothing_is_lost(chunked)
