"""The licence travels with the weights (ADR 0009 §3).

Three things live here because they are facts about a licence, not about an
entry: the canonical text the app bundles for every id on the allowlist, how
to recognise each licence from an upstream's own LICENSE file, and which
files the store writes into a promoted directory from the app's own table
rather than from the network.
"""

import itertools
import re
from bisect import bisect_left
from dataclasses import dataclass
from functools import cache
from typing import Self

from readily_engine.catalog.licence_table import (
    HEADINGS,
    LICENCE_OBLIGATIONS,
    TERMS,
    Terms,
    is_licence_file,
    is_notice_file,
)
from readily_engine.catalog.manifest import MANIFEST_PATH, PinnedArtifact

# Baked in beside the manifest (ADR 0003 §1): the same path resolves in the
# repo and in the app bundle, so the Engine never fetches a licence text.
LICENSES_DIR = MANIFEST_PATH.parent / "licenses"

# A licence is recognised by its complete terms, normalised to letters and
# digits so an upstream's markdown, quotes and line breaks fall away. Letters
# and digits in every script, not ASCII's: a term written in another script
# has to stand as text the file answers for, not fall away as punctuation.
_MARKS_AT_THE_START = re.compile(r"[^a-zA-Z0-9]*")
_MARKS_AT_THE_END = re.compile(r"[^a-zA-Z0-9]*$")
# What a blank holds, and what stands beside the terms, is read back in the
# upstream's own punctuation and case and compared with the notice the entry
# declares, line for line. A name and an added restriction are the same
# letters once squashed, and no grammar tells a licensor's name from a term
# written in Title Case, so the curator who copied the lines into the draft
# is the check on what they say; this is the proof that the file still says
# them and nothing else.


@dataclass(frozen=True)
class _Squashed:
    """A text reduced to its letters and digits, remembering where each of
    them came from so a passage matched here can be read back in full."""

    text: str
    at: tuple[int, ...]
    source: str

    def between(self, ends: int, starts: int) -> str:
        """What the source says between two squashed positions."""
        return self.source[self.at[ends - 1] + 1 : self.at[starts]]

    def line_around(self, position: int) -> tuple[str, int]:
        """The source line holding a squashed position, and where the
        source continues after it."""
        index = self.at[position]
        start = self.source.rfind("\n", 0, index) + 1
        end = self.source.find("\n", index)
        if end < 0:
            end = len(self.source)
        return self.source[start:end], end + 1


@dataclass(frozen=True)
class _Passages:
    """A stretch of a bundled text, squashed and split where its blanks
    leave it, with the punctuation the text sets around each blank — the
    quotes around BSD's "AS IS" — so what a licensor wrote into the blank
    can be read back without the licence's own marks on it."""

    squashed: tuple[str, ...]
    framing: tuple[tuple[str, str], ...]

    @classmethod
    def cut(cls, text: str, at_a_blank: str) -> Self:
        pieces = re.split(at_a_blank, text)
        return cls(
            squashed=tuple(map(_squash, pieces)),
            framing=tuple(
                (
                    _MARKS_AT_THE_END.search(before)[0].strip(),
                    _MARKS_AT_THE_START.match(after)[0].strip(),
                )
                for before, after in itertools.pairwise(pieces)
            ),
        )


@dataclass(frozen=True)
class _Shape:
    """One licence's bundled text, squashed and cut at its terms: what an
    upstream may quote before them, must quote in full, may quote after,
    each split where a blank leaves it, and the blanks' own phrases."""

    licence: str
    before: str
    passages: _Passages
    after: _Passages
    blanks: frozenset[str]


def licence_text(licence: str) -> str:
    """The bundled canonical text for an allowlisted licence id."""
    return (LICENSES_DIR / f"{licence}.txt").read_text(encoding="utf-8")


def licence_text_for(entry: PinnedArtifact) -> str:
    """Bundled terms, with a sourced holder line when upstream supplied none."""
    text = licence_text(entry.license)
    obligations = LICENCE_OBLIGATIONS[entry.license]
    if obligations.requires_holder_notice and not any(
        is_licence_file(file.path) for file in entry.files
    ):
        assert entry.copyright_notice is not None
        return (
            f"{obligations.display_name} License\n\n{entry.copyright_notice}\n\n{text}"
        )
    return text


def identify_licence(text: str, copyright_notice: str | None = None) -> str | None:
    """Which allowlisted licence `text` is, or None when it is none of them.

    The file has to be the licence and nothing else: its terms in full,
    the framing the bundled text shows around them — a title, Apache's
    appendix, Creative Commons' disclaimer — and, where the licence leaves
    room for the holder's notice, exactly `copyright_notice` (see
    `read_licence`). A term written above or below the terms binds the
    reader as much as one written into them, so it makes the file some
    other licence, and so does a notice the curator did not declare. What
    an upstream may add below its own licence is the third-party notices
    for what it vendored, each the same shape again under a heading.
    Between the licences a file then quotes, an agreement that binds the
    reader is the licence wherever it sits, since the permissive blocks
    beside it can only be those notices; between permissive licences, the
    first is the licence. A None is a refusal, not a guess: a draft whose
    upstream LICENSE file is unrecognised is refused the same as one that
    names a different licence than the draft declares.
    """
    licence, written = read_licence(text)
    return licence if written == _as_read(copyright_notice) else None


def read_licence(text: str) -> tuple[str | None, str | None]:
    """Which allowlisted licence `text` quotes, and what it says where the
    licence leaves room for the holder's notice.

    The notice is every line beside the terms that is neither a title nor
    a heading over third-party notices, and whatever fills a blank the
    licensor did not leave as the licence wrote it, in the file's order,
    one per line and the whitespace around each gone; None when the file
    says nothing there. Both are None when the text is no licence. This is
    what `identify_licence` compares with the entry's `copyright_notice`,
    read back on its own so a refusal can show the curator the lines to
    declare.
    """
    blocks, written = _walk(text)
    licence = _the_licence(blocks)
    if licence is None:
        return None, None
    return licence, "\n".join(written) or None


def files_the_store_writes(entry: PinnedArtifact) -> dict[str, str]:
    """The files the store generates beside a promoted model, by name.

    `LICENSE` carries the bundled text unless the entry pins upstream's own
    licence file, which is the better copy: hash-verified and carrying its
    own copyright lines. For MIT/BSD without that file, the declared holder
    line is written above the stock terms. `Notice` carries the exact
    attribution string the licence prescribes (Llama §1.b.iii) unless the
    entry pins upstream's own, which curation has read and found to say
    exactly that: the store never writes over a pinned file.
    """
    obligations = LICENCE_OBLIGATIONS[entry.license]
    written: dict[str, str] = {}
    if not any(is_licence_file(file.path) for file in entry.files):
        written["LICENSE"] = licence_text_for(entry)
    if obligations.notice is not None and not any(
        is_notice_file(file.path) for file in entry.files
    ):
        written["Notice"] = obligations.notice
    return written


def says_the_prescribed_notice(text: str, licence: str) -> bool:
    """Whether a notice file says exactly what `licence` prescribes its
    distributors write (Llama §1.b.iii), line by line, layout aside."""
    prescribed = LICENCE_OBLIGATIONS[licence].notice
    return prescribed is not None and _lines(text) == _lines(prescribed)


def _the_licence(blocks: list[str]) -> str | None:
    if not blocks:
        return None
    binding = [b for b in blocks if LICENCE_OBLIGATIONS[b].binds_reader]
    if len(binding) > 1:
        return None
    return binding[0] if binding else blocks[0]


def _walk(text: str) -> tuple[list[str], list[str]]:
    """The licences `text` quotes, in order, and every line it writes that
    is neither a licence's terms and framing nor a title or heading."""
    squashed = _squashed(text)
    blocks: list[str] = []
    written: list[str] = []
    position = 0
    while (start := bisect_left(squashed.at, position)) < len(squashed.at):
        if (quoted := _quoted_at(squashed, start)) is not None:
            licence, end, fillings = quoted
            if licence is not None:
                blocks.append(licence)
            written.extend(fillings)
            position = squashed.at[end - 1] + 1
            continue
        line, position = squashed.line_around(start)
        if _squash(line) not in _titles_and_headings():
            written.extend(_lines(line))
    return blocks, written


def _quoted_at(
    squashed: _Squashed, start: int
) -> tuple[str | None, int, list[str]] | None:
    """The licence whose terms `squashed` quotes from `start`, with its
    trailing framing, where they end and what fills their blanks; or a
    licence's leading framing alone, as a None licence; or None when
    nothing is quoted there."""
    for shape in _shapes():
        if (terms := _quotes(squashed, shape, shape.passages, start)) is not None:
            end, fillings = terms
            if (after := _quotes(squashed, shape, shape.after, end)) is not None:
                end, more = after
                fillings += more
            return shape.licence, end, fillings
        if shape.before and squashed.text.startswith(shape.before, start):
            return None, start + len(shape.before), []
    return None


@cache
def _shapes() -> tuple[_Shape, ...]:
    """Each licence's squashed text cut at its terms, the terms split where
    a blank leaves them."""
    shapes = []
    for licence in LICENCE_OBLIGATIONS:
        terms = TERMS.get(licence, Terms())
        text = licence_text(licence)
        first = 0 if terms.start is None else text.index(terms.start)
        last = len(text)
        if terms.end is not None:
            last = text.index(terms.end) + len(terms.end)
        blanks = sorted(terms.blanks, key=len, reverse=True)
        at_a_blank = "|".join(map(re.escape, blanks)) or "(?!)"
        shapes.append(
            _Shape(
                licence,
                before=_squash(text[:first]),
                passages=_Passages.cut(text[first:last], at_a_blank),
                after=_Passages.cut(text[last:], at_a_blank),
                blanks=frozenset(map(_squash, blanks)),
            )
        )
    return tuple(shapes)


@cache
def _titles_and_headings() -> frozenset[str]:
    titles = (title for terms in TERMS.values() for title in terms.titles)
    return HEADINGS | frozenset(map(_squash, titles))


def _quotes(
    squashed: _Squashed, shape: _Shape, passages: _Passages, start: int
) -> tuple[int, list[str]] | None:
    """Where `squashed` stops quoting the passages, if they follow `start`
    in order, and what the licensor wrote into each blank between them that
    it did not leave as the licence wrote it."""
    first, *rest = passages.squashed
    if not squashed.text.startswith(first, start):
        return None
    end = start + len(first)
    fillings: list[str] = []
    for passage, (opens, closes) in zip(rest, passages.framing, strict=True):
        found = squashed.text.find(passage, end)
        if found < 0:
            return None
        filling = squashed.between(end, found).strip()
        if _squash(filling) not in shape.blanks:
            fillings.extend(_lines(filling.removeprefix(opens).removesuffix(closes)))
        end = found + len(passage)
    return end, fillings


def _lines(text: str) -> list[str]:
    """`text` line by line, the whitespace around each gone and blank lines
    dropped: how a file lays a notice out is not what the notice says."""
    return [line for raw in text.splitlines() if (line := raw.strip())]


def _as_read(copyright_notice: str | None) -> str | None:
    """A declared notice in the form `read_licence` reads one back."""
    if copyright_notice is None:
        return None
    return "\n".join(_lines(copyright_notice)) or None


def _squashed(text: str) -> _Squashed:
    # Character by character so each squashed letter keeps its source
    # index, and a letter that folds to several (a sharp s) keeps it on
    # each of them.
    kept = [
        (index, folded)
        for index, character in enumerate(text)
        for folded in character.casefold()
        if folded.isalnum()
    ]
    return _Squashed(
        text="".join(character for _, character in kept),
        at=tuple(index for index, _ in kept),
        source=text,
    )


def _squash(text: str) -> str:
    return _squashed(text).text
