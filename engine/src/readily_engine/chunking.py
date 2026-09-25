"""The chunker: an arbitrary Source becomes ordered Blocks, losing nothing.

Chunking is mandatory, not an optimisation: quality degrades with length for
two independent reasons — hard context windows (Kokoro raises past 510
phoneme tokens) and autoregressive drift (Chatterbox-class corrupts past
~25-30s) — so every Voice Model gets text in bites it narrates well. ADR 0002
§2 fixes the shape: normalize, split paragraphs, sentence-segment with pysbd,
greedily pack sentences up to the Catalog entry's character budget, and split
an oversized sentence by punctuation waterfall.

Deterministic code, no LLM anywhere in this path. Boundaries here are
syntactic, not semantic; an LLM would add seconds to a latency-critical path
and its non-determinism would break the Segment cache, which is keyed on
exact Block text.

**Silent text loss is the one forbidden outcome.** `normalize` is the single
place a character may change, and what it returns is the Source that Blocks
index into and History stores. After it, the chunker only ever *cuts*: every
Block's text is a literal slice of that normalized Source, the slices run in
order without overlapping, and the only thing between two of them is
whitespace. `tests/test_chunking.py` states that as a property over arbitrary
input.

Pure and offline: no filesystem, no network, no model loading, no `mlx` — so
it tests in full on ubuntu CI, which is where the no-loss property has to be
provable (engine/README.md § "Test layout").
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

import pysbd

from readily_engine.catalog import Tunables

# The punctuation waterfall, in priority order: sentence end, then clause,
# then comma/dash. A sentence too long for the budget is cut at the last
# break of the best tier that appears in the window — the same policy as
# Kokoro's own `waterfall_last` (ADR 0002 §2).
_WATERFALL = ("!.?…", ":;", ",—")

# A normalized Source separates paragraphs with exactly this, so a maximal
# run of non-newline characters is exactly one paragraph.
_PARAGRAPH = re.compile(r"[^\n]+")

# pysbd's cost grows faster than a paragraph's length (about 2 ms per
# thousand characters at 2k, 12 ms at 32k), and a hard-wrapped paste
# normalizes to one paragraph the size of the whole Source, so a paragraph
# is handed over in windows of at most this many characters.
_WINDOW_CHARS = 2000


class Boundary(StrEnum):
    """How a Block ends — which is what the assembler needs to know to make
    the seam after it audible (ADR 0002 §4): a pause between paragraphs, a
    shorter one between sentences, and nothing at all mid-sentence, where the
    waterfall had to cut and only a butt-join (or the emergency crossfade)
    belongs. The last Block of a Source ends its paragraph like any other.
    """

    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    MID_SENTENCE = "mid-sentence"


@dataclass(frozen=True)
class Block:
    """A contiguous run of a Source's text, cut at natural speech boundaries
    and sized so a Voice Model can narrate it in one pass (CONTEXT.md).

    `start` and `end` are the Source character range ADR 0002 §7 requires
    Segment metadata to carry — read-along now, scrubbing in v1. They index
    the *normalized* Source (`ChunkedSource.source`), and `text` is exactly
    that slice: `source[block.start : block.end] == block.text`.
    """

    text: str
    start: int
    end: int
    boundary: Boundary


@dataclass(frozen=True)
class ChunkedSource:
    """A Source normalized and cut into Blocks.

    The normalized text travels with the Blocks because it is what their
    ranges are ranges *of*: store this `source` as the Narration's Source and
    every range stays meaningful for as long as the record lives.
    """

    source: str
    blocks: tuple[Block, ...]


def chunk(source: str, tunables: Tunables) -> ChunkedSource:
    """Cut `source` into Blocks under the Catalog entry's budgets.

    The budgets are the entry's rather than the pipeline's because they are
    properties of the model — how much text it narrates well in one pass.
    The first Block is one sentence and at most `first_block_chars`, so first
    audio costs one short inference; every other Block packs as many whole
    sentences as `chunk_budget_chars` allows and never crosses a paragraph.
    """
    normalized = normalize(source)
    sentences = _sentences(normalized)
    if not sentences:
        return ChunkedSource(normalized, ())

    units = _first_units(normalized, sentences[0], tunables)
    for sentence in sentences[1:]:
        units.extend(
            _split_to_budget(normalized, sentence, tunables.chunk_budget_chars)
        )

    blocks = [_block(normalized, [units[0]])]
    pending: list[_Unit] = []
    for unit in units[1:]:
        same_paragraph = pending and unit.paragraph == pending[0].paragraph
        if (
            same_paragraph
            and unit.end - pending[0].start <= tunables.chunk_budget_chars
        ):
            pending.append(unit)
            continue
        if pending:
            blocks.append(_block(normalized, pending))
        pending = [unit]
    if pending:
        blocks.append(_block(normalized, pending))
    return ChunkedSource(normalized, tuple(blocks))


def normalize(source: str) -> str:
    """Put a Source in the one canonical shape the chunker cuts.

    Paragraphs end up separated by exactly one blank line and words by exactly
    one space, because a soft line wrap is not a paragraph break: text pasted
    out of a PDF arrives wrapped at the column the PDF happened to use, and
    honouring those newlines would chop it into one Block per printed line.
    Blank lines survive — they are the strongest prosodic boundary there is.

    This is the one lossy step in the pipeline, and it is lossy only about
    things nobody hears: Unicode is folded to NFC, every other flavour of
    whitespace becomes a plain space, and control and format characters —
    zero-width spaces, bidi marks, the BOM, the soft hyphens PDF extraction
    leaves behind — are dropped rather than handed to a phonemizer.
    """
    text = unicodedata.normalize("NFC", source)
    # U+2028/U+2029 are the two separators a phonemizer will not see as
    # line breaks; a paste out of a word processor is where they come from.
    text = re.sub("\r\n|\r|\u2028", "\n", text)
    text = text.replace("\u2029", "\n\n")
    text = "".join(
        " " if character != "\n" and character.isspace() else character
        for character in text
        if character == "\n" or character.isspace() or not _invisible(character)
    )
    text = re.sub(r" +", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return text.strip()


def has_text(source: str) -> bool:
    """Whether `normalize(source)` would keep anything but whitespace, found
    without normalizing it: the first such character settles it, so a request
    validator can ask this of a million-character body on the event loop."""
    return any(
        not character.isspace() and not _invisible(character) for character in source
    )


_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf"})


def _invisible(character: str) -> bool:
    """Control and format characters: zero-width spaces, bidi marks, the BOM,
    soft hyphens. `normalize` drops them and `has_text` looks past them."""
    return unicodedata.category(character) in _INVISIBLE_CATEGORIES


@dataclass(frozen=True)
class _Unit:
    """One packable run of the normalized Source: a whole sentence, or one
    piece of a sentence the waterfall had to cut. `ends_sentence` is false
    for every piece but the last, which is what makes a Block's seam
    mid-sentence."""

    start: int
    end: int
    paragraph: int
    ends_sentence: bool


@dataclass(frozen=True)
class _Sentence:
    start: int
    end: int
    paragraph: int
    # False for a sentence a window seam cut short of any punctuation, so
    # the Block ending there gets a butt-join rather than a sentence pause.
    ends_sentence: bool = True


def _sentences(source: str) -> list[_Sentence]:
    """Sentence-segment every paragraph of a normalized Source.

    pysbd supplies the boundaries; the cutting is ours, and that is
    deliberate. pysbd re-locates each sentence in the original text with a
    regex search (`Segmenter.sentences_with_char_spans`), so a sentence its
    processor rewrote internally is silently *dropped* from the result — the
    one outcome this pipeline may not have. Taking only the offsets it agrees
    with and slicing ourselves makes a lost boundary cost a merged sentence,
    which the packer and the waterfall then handle like any other long one.
    """
    # Per call, not per module: `Segmenter.segment` stores the text it is
    # working on on the instance, so a shared one is not safe to call from
    # two threads.
    segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
    sentences: list[_Sentence] = []
    cursor = 0
    for index, match in enumerate(_PARAGRAPH.finditer(source)):
        paragraph = match.group()
        spans: list[tuple[int, int]] = []
        seams: list[int] = []
        for window_start, window_end in _windows(paragraph):
            for start, end in _sentence_spans(
                segmenter, paragraph[window_start:window_end]
            ):
                spans.append((window_start + start, window_start + end))
            if window_end < len(paragraph):
                seams.append(len(spans) - 1)
        # A window seam is chosen by punctuation, and "Mr." has that
        # punctuation too. Only pysbd can say whether the seam ends a
        # sentence, so it is asked about the two sentences the seam joins.
        ends_sentence = [True] * len(spans)
        for at in seams:
            (start, seam), (_, following_end) = spans[at], spans[at + 1]
            ends_sentence[at] = any(
                start + end == seam
                for _, end in _sentence_spans(segmenter, paragraph[start:following_end])
            )
        for sentence_index, ((start, end), ends) in enumerate(
            zip(spans, ends_sentence, strict=True)
        ):
            # The first sentence owns the paragraph separator before it. The
            # first sentence of the Source starts at zero, so Block ranges tile
            # the normalized Source exactly without special-case metadata.
            absolute_start = cursor if sentence_index == 0 else match.start() + start
            sentences.append(
                _Sentence(absolute_start, match.start() + end, index, ends)
            )
        cursor = match.end()
    return sentences


def _windows(paragraph: str) -> list[tuple[int, int]]:
    """Slices of at most `_WINDOW_CHARS` that tile `paragraph`, for pysbd to
    segment one at a time.

    A seam falls after sentence punctuation or before a space, so the
    separator leads the next window exactly as it leads the next sentence.
    A seam at an abbreviation splits a sentence across two windows; the
    caller asks pysbd whether the seam ends one before making it a pause.
    """
    windows: list[tuple[int, int]] = []
    start = 0
    while len(paragraph) - start > _WINDOW_CHARS:
        limit = start + _WINDOW_CHARS
        end = limit
        for index in range(limit - 1, start, -1):
            if paragraph[index] in _WATERFALL[0] and paragraph[index + 1] == " ":
                end = index + 1
                break
        else:
            space = paragraph.rfind(" ", start + 1, limit)
            if space > start:
                end = space
        windows.append((start, end))
        start = end
    windows.append((start, len(paragraph)))
    return windows


def _sentence_spans(
    segmenter: pysbd.Segmenter, paragraph: str
) -> list[tuple[int, int]]:
    """Sentence spans that tile `paragraph` without losing their separators.

    pysbd includes trailing whitespace in a sentence. Cutting just before it
    makes that separator the leading edge of the next sentence, so a Block
    boundary remains a lossless Source boundary while synthesis can strip the
    separator at its own boundary.
    """
    cuts: list[int] = []
    for span in segmenter.segment(paragraph):
        if paragraph[span.start : span.end] != span.sent:
            continue  # a span pysbd could not place; the boundary is lost, not the text
        end = span.end
        while end > span.start and paragraph[end - 1].isspace():
            end -= 1
        if 0 < end < len(paragraph) and end > (cuts[-1] if cuts else 0):
            cuts.append(end)

    spans: list[tuple[int, int]] = []
    bounds = [0, *cuts, len(paragraph)]
    for start, end in pairwise(bounds):
        if paragraph[start:end].strip():
            spans.append((start, end))
    return spans


def _first_units(source: str, sentence: _Sentence, tunables: Tunables) -> list[_Unit]:
    """The units of the first sentence, whose head is the first Block.

    ADR 0002 §2 caps the first Block at one short sentence so time-to-first
    audio is one short inference. A first sentence longer than that budget is
    waterfalled at *it* — and only its head is; what remains goes back on the
    normal budget rather than being minced into 150-character Blocks.
    """
    if sentence.end - sentence.start <= tunables.first_block_chars:
        return [
            _Unit(
                sentence.start, sentence.end, sentence.paragraph, sentence.ends_sentence
            )
        ]
    cut = _waterfall(source, sentence.start, tunables.first_block_chars)
    rest = _Sentence(cut, sentence.end, sentence.paragraph, sentence.ends_sentence)
    return [
        _Unit(sentence.start, cut, sentence.paragraph, False),
        *_split_to_budget(source, rest, tunables.chunk_budget_chars),
    ]


def _split_to_budget(source: str, sentence: _Sentence, budget: int) -> list[_Unit]:
    """One unit per sentence that fits; waterfall pieces for one that does not."""
    units: list[_Unit] = []
    start = sentence.start
    while sentence.end - start > budget:
        cut = _waterfall(source, start, budget)
        units.append(_Unit(start, cut, sentence.paragraph, False))
        start = cut
    units.append(_Unit(start, sentence.end, sentence.paragraph, sentence.ends_sentence))
    return units


def _waterfall(source: str, start: int, budget: int) -> int:
    """Where to cut a run of `source` that starts at `start` and is longer
    than `budget`, as an exclusive end no further than `start + budget`.

    Sentence-ending punctuation first, then clause, then comma or dash, then
    the last space — and, for the run with none of those (a 500-character URL
    is the honest example), a cut at the budget. That last resort is not
    pretty, but the alternative is dropping characters, and dropping
    characters is the thing this pipeline never does.
    """
    limit = start + budget
    for tier in _WATERFALL:
        cut = _last_break(source, start, limit, tier)
        if cut > start:
            return cut
    space = source.rfind(" ", start, limit)
    return space if space > start else limit


def _last_break(source: str, start: int, limit: int, tier: str) -> int:
    """The exclusive end just past the last `tier` character in the window,
    or `start` if the window holds none."""
    for index in range(limit - 1, start - 1, -1):
        if source[index] in tier and not _is_decimal_point(source, index):
            return index + 1
    return start


def _is_decimal_point(source: str, index: int) -> bool:
    """Whether the character at `index` is the point inside a number. Cutting
    there would hand a phonemizer "3." and "14" and get "three point" read as
    a whole sentence."""
    return (
        source[index] == "."
        and index > 0
        and source[index - 1].isdigit()
        and index + 1 < len(source)
        and source[index + 1].isdigit()
    )


def _block(source: str, units: list[_Unit]) -> Block:
    """One Block over consecutive units of a single paragraph."""
    start, end = units[0].start, units[-1].end
    if not units[-1].ends_sentence:
        boundary = Boundary.MID_SENTENCE
    elif end == len(source) or source[end : end + 1] == "\n":
        boundary = Boundary.PARAGRAPH
    else:
        boundary = Boundary.SENTENCE
    return Block(source[start:end], start, end, boundary)
