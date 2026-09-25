"""What each licence on the Catalog's allowlist is, in two tables (ADR 0009).

Facts about a licence, never about an entry, so curation cannot fill them
in. A dependency leaf: the manifest schema validates entries against
`LICENCE_OBLIGATIONS` (§2's table, what a licence obliges Readily to carry
with the weights) and licence recognition reads an upstream's file against
`TERMS` (where in each bundled text the licence's terms are, §3), and
neither module needs the other to do so.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

# The exact names upstreams give their licence and notice files, in each
# case they spell them, since Hugging Face paths are case-sensitive.
# Curation asks for every one of these alongside a draft's own files; the
# list is exact names rather than a pattern because it reaches the
# downloader as allow-patterns, and a broader match would fetch files the
# draft never named.
LICENCE_FILE_NAMES = tuple(
    f"{stem}{suffix}"
    for stem in ("LICENSE", "License", "license", "NOTICE", "Notice", "notice")
    for suffix in ("", ".md", ".txt")
)
_LICENCE_NAMES = frozenset(
    name.casefold()
    for name in LICENCE_FILE_NAMES
    if name.casefold().startswith("license")
)


def is_licence_file(path: str) -> bool:
    """Whether a manifest path is a licence text by name, in any directory.

    A NOTICE is not one: it names other people's licences by design. Nor is
    a weights file that happens to start with the word.
    """
    return PurePosixPath(path).name.casefold() in _LICENCE_NAMES


def is_notice_file(path: str) -> bool:
    """Whether a manifest path is the `Notice` a licence prescribes by name
    (Llama §1.b.iii spells it so), in any directory.

    Only the bare stem: `NOTICE.md` or `NOTICE.txt` is a third-party notice
    of upstream's own, pinned like any other file and never stood in for it.
    """
    return PurePosixPath(path).name.casefold() == "notice"


@dataclass(frozen=True, slots=True)
class ReaderAttributionObligations:
    warranty_notice: str


@dataclass(frozen=True, slots=True)
class LicenceObligations:
    """What one allowlisted licence obliges Readily to do when it hands the
    weights to a reader (ADR 0009 §2's table, one row).

    Text fields rather than flags because other code writes the strings
    out: the store writes `notice` into a `Notice` file next to the
    weights, and the sheet prints `display_name`, `credit`, and the reader
    attribution fields. The `binds_reader` field turns on the "downloading
    accepts" clause. A fact about the licence, never about the entry, so
    curation cannot fill it in.
    """

    display_name: str
    # The exact text the licence prescribes for a `Notice` file shipped with
    # the weights (Llama §1.b.iii), or None when it prescribes
    # none. The licence's own words, not a paraphrase.
    notice: str | None
    # The line the licence asks the product to display (Llama §1.b.i(B)).
    credit: str | None
    reader_attribution: ReaderAttributionObligations | None
    # Whether the licence reaches the reader and not only Readily: a use
    # policy the reader inherits by running the model. Accepted by
    # downloading, and the sheet says so (ADR 0009 §4).
    binds_reader: bool
    # Whether what Readily must ship is the licensor's own copyright notice
    # (ADR 0009 §2: MIT and BSD, "the notice"). Curation pins upstream's
    # licence file when one exists. Otherwise the entry records the reviewed
    # source of its declared holder line and the store writes that line above
    # the bundled terms (ADR 0009's 2026-09-03 amendment).
    requires_holder_notice: bool

    @property
    def requires_attribution(self) -> bool:
        return self.reader_attribution is not None


def _llama(version: str) -> LicenceObligations:
    return LicenceObligations(
        display_name=f"Llama {version} Community License",
        notice=(
            f"Llama {version} is licensed under the Llama {version} Community "
            "License, Copyright © Meta Platforms, Inc. All Rights Reserved."
        ),
        credit="Built with Llama",
        reader_attribution=None,
        binds_reader=True,
        requires_holder_notice=False,
    )


def _no_obligations_on_the_reader(
    display_name: str,
    *,
    requires_holder_notice: bool = False,
    reader_attribution: ReaderAttributionObligations | None = None,
) -> LicenceObligations:
    return LicenceObligations(
        display_name=display_name,
        notice=None,
        credit=None,
        reader_attribution=reader_attribution,
        binds_reader=False,
        requires_holder_notice=requires_holder_notice,
    )


def _openrail_m(display_name: str) -> LicenceObligations:
    return LicenceObligations(
        display_name=display_name,
        notice=None,
        credit=None,
        reader_attribution=None,
        binds_reader=True,
        requires_holder_notice=False,
    )


def _the_notice(display_name: str) -> LicenceObligations:
    return _no_obligations_on_the_reader(display_name, requires_holder_notice=True)


# The licences a Voice Model may ship under: every licence that lets Readily
# redistribute the weights to an end user for ordinary personal use, and what
# each obliges Readily to carry with them (ADR 0009 §2). Model weights are
# not linked into a shipped process, so they miss every dependency gate, and
# the bar here is not the dependency policy's "permissive": a licence that
# binds the reader (Llama, OpenRAIL-M) is admitted, and the app
# discharges its obligations, while a scope narrower than personal use
# (non-commercial, research-only) and copyleft weights stay out. Keys are
# SPDX ids where SPDX has one and Hugging Face's `license` tag where it does
# not, because the model card is where curation reads them. Enforced on the
# manifest schema rather than in the curation script so both roads meet it:
# the script refuses the draft before a byte downloads, and CI refuses a
# hand-edited entry when it parses the committed manifest. The list moves
# only by an ADR amending 0009, never by a widened literal here.
LICENCE_OBLIGATIONS: Mapping[str, LicenceObligations] = MappingProxyType(
    {
        "MIT": _the_notice("MIT"),
        "BSD-2-Clause": _the_notice("BSD 2-Clause"),
        "BSD-3-Clause": _the_notice("BSD 3-Clause"),
        "CC0-1.0": _no_obligations_on_the_reader("CC0 1.0"),
        "Apache-2.0": _no_obligations_on_the_reader("Apache 2.0"),
        "CC-BY-4.0": _no_obligations_on_the_reader(
            "Creative Commons Attribution 4.0",
            reader_attribution=ReaderAttributionObligations(
                warranty_notice=(
                    "Section 5 \N{EN DASH} Disclaimer of Warranties and "
                    "Limitation of Liability."
                )
            ),
        ),
        "llama3.1": _llama("3.1"),
        "llama3.2": _llama("3.2"),
        "llama3.3": _llama("3.3"),
        "llama4": _llama("4"),
        "bigscience-openrail-m": _openrail_m("BigScience OpenRAIL-M"),
        "creativeml-openrail-m": _openrail_m("CreativeML OpenRAIL-M"),
    }
)


# What an upstream writes over the third-party notices it appends below
# its own licence. Anything else is a term, until a curator says otherwise.
HEADINGS = frozenset(
    {
        "thirdparty",
        "thirdpartynotices",
        "thirdpartylicenses",
        "thirdpartylicences",
        "thirdpartysoftware",
        "notices",
        "licenses",
        "licences",
    }
)


@dataclass(frozen=True)
class Terms:
    """Where in a bundled text the licence's terms are: the part an
    upstream's own file has to quote in full.

    Outside `start`..`end` is framing that is not the licence and that
    upstreams leave out: Creative Commons' disclaimer around its public
    licence, Meta's use policy, which its repos ship as a separate file,
    Apache's appendix template. A blank is a phrase the licence itself
    tells the licensor to replace with their own name, in the terms or in
    the framing after them. A title is a name upstreams write over terms
    whose bundled text carries none.
    """

    start: str | None = None
    end: str | None = None
    blanks: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()


_LLAMA_AGREEMENT = Terms(end="dispute arising out of this Agreement.")
_BSD_TITLES = ("BSD License", "The BSD License")
TERMS = {
    "MIT": Terms(
        blanks=("THE AUTHORS OR COPYRIGHT HOLDERS",),
        titles=("MIT", "MIT License", "The MIT License", "The MIT License (MIT)"),
    ),
    "BSD-2-Clause": Terms(
        blanks=(
            "THE COPYRIGHT HOLDERS AND CONTRIBUTORS",
            "THE COPYRIGHT HOLDER OR CONTRIBUTORS",
        ),
        titles=(
            *_BSD_TITLES,
            "BSD 2-Clause License",
            "The 2-Clause BSD License",
            'BSD 2-Clause "Simplified" License',
            "Simplified BSD License",
        ),
    ),
    "BSD-3-Clause": Terms(
        blanks=(
            "the copyright holder",
            "THE COPYRIGHT HOLDERS AND CONTRIBUTORS",
            "THE COPYRIGHT HOLDER OR CONTRIBUTORS",
        ),
        titles=(
            *_BSD_TITLES,
            "BSD 3-Clause License",
            "The 3-Clause BSD License",
            'BSD 3-Clause "New" or "Revised" License',
            "New BSD License",
            "Modified BSD License",
        ),
    ),
    "Apache-2.0": Terms(
        end="END OF TERMS AND CONDITIONS",
        blanks=("[yyyy] [name of copyright owner]",),
    ),
    "CC-BY-4.0": Terms(
        start="Creative Commons Attribution 4.0 International Public License",
        end="including from the legal processes of any jurisdiction or authority.",
        titles=("Creative Commons Attribution 4.0 International", "CC BY 4.0"),
    ),
    "llama3.1": _LLAMA_AGREEMENT,
    "llama3.2": _LLAMA_AGREEMENT,
    "llama3.3": _LLAMA_AGREEMENT,
    "llama4": _LLAMA_AGREEMENT,
}
