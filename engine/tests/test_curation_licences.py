"""Curation pins the licence files upstream ships, and reads them.

Every case here runs `pin` against an in-memory upstream: which licence and
notice names are fetched and pinned, and what curation refuses a draft for
when the file does not say what the draft declares (ADR 0009 §3). The
matcher itself is covered in test_catalog_licences.py; this is its seam
with curation.
"""

from pathlib import Path

import pytest
from conftest import (
    ACMES_NOTICE,
    FILES,
    MIT_WITH_A_HOLDER,
    draft_for,
    fetch_writing,
    make_entry,
)

from readily_engine.catalog import LICENCE_OBLIGATIONS
from readily_engine.catalog.licences import licence_text
from readily_engine.curation import CurationError, Draft, pin


def test_pinning_also_pins_the_licence_files_upstream_ships(tmp_path: Path) -> None:
    upstream = {
        **FILES,
        "LICENSE": licence_text("Apache-2.0").encode(),
        "NOTICE": b"Acme Kokoro\nCopyright 2024 Acme",
    }

    pinned = pin(draft_for(), tmp_path, fetch_writing(upstream))

    assert [file.path for file in pinned] == [*FILES, "LICENSE", "NOTICE"]
    assert list(pinned) == make_entry(files=upstream).files


@pytest.mark.parametrize("name", ["LICENSE", "license.md", "License.txt"])
def test_a_licence_file_left_by_an_earlier_revision_is_not_pinned(
    tmp_path: Path, name: str
) -> None:
    with_licence = {**FILES, name: licence_text("Apache-2.0").encode()}
    pin(draft_for(), tmp_path, fetch_writing(with_licence))

    pinned = pin(draft_for(), tmp_path, fetch_writing(FILES))

    assert [file.path for file in pinned] == list(FILES)
    assert not (tmp_path / name).exists()


def test_a_licence_file_is_found_in_whatever_case_upstream_spells_it(
    tmp_path: Path,
) -> None:
    upstream = {**FILES, "license": licence_text("Apache-2.0").encode()}

    pinned = pin(draft_for(), tmp_path, fetch_writing(upstream))

    assert [file.path for file in pinned] == [*FILES, "license"]


def test_a_licence_file_upstream_has_since_dropped_is_not_pinned_again(
    tmp_path: Path,
) -> None:
    # Curation owns the licence file: the entry pins it, but the draft
    # read back from that entry never names it, so re-curation asks
    # upstream for it afresh and a revision that dropped it drops it.
    upstream = {**FILES, "LICENSE": licence_text("Apache-2.0").encode()}
    pinned = pin(draft_for(), tmp_path, fetch_writing(upstream))
    entry = draft_for().entry(pinned, {})
    assert [file.path for file in entry.files] == [*FILES, "LICENSE"]

    draft = Draft.from_entry(entry)
    pinned = pin(draft, tmp_path, fetch_writing(FILES))

    assert draft.files == list(FILES)
    assert [file.path for file in pinned] == list(FILES)


def test_the_notice_upstream_wrote_has_to_be_the_one_the_draft_declares(
    tmp_path: Path,
) -> None:
    # The bundled MIT text names nobody; upstream's file names the
    # holder, and the draft says who, so a refusal shows the curator the
    # lines the file carries where that notice sits, to copy across.
    upstream = {**FILES, "LICENSE": licence_text("MIT").encode()}
    draft = Draft.from_entry(
        make_entry(files=upstream, license="MIT", copyright_notice=ACMES_NOTICE)
    )

    with pytest.raises(
        CurationError, match=r"LICENSE(.|\n)*nothing(.|\n)*Acme"
    ) as refused:
        pin(draft, tmp_path, fetch_writing(upstream))
    assert "not a licence" not in str(refused.value)

    upstream["LICENSE"] = b"Copyright (c) 2024 Acme Inc.\n" + upstream["LICENSE"]
    with pytest.raises(CurationError) as refused:
        pin(draft, tmp_path, fetch_writing(upstream))
    assert "Copyright (c) 2024 Acme Inc." in str(refused.value)
    assert ACMES_NOTICE in str(refused.value)

    upstream["LICENSE"] = MIT_WITH_A_HOLDER
    pinned = pin(draft, tmp_path, fetch_writing(upstream))
    assert [file.path for file in pinned] == [*FILES, "LICENSE"]


def test_a_pinned_licence_file_supersedes_the_draft_s_copyright_source(
    tmp_path: Path,
) -> None:
    upstream = {**FILES, "LICENSE": MIT_WITH_A_HOLDER}
    draft = draft_for(
        license="MIT",
        copyright_notice=ACMES_NOTICE,
        copyright_source="https://example.com/acme/LICENSE",
    )

    pinned = pin(draft, tmp_path, fetch_writing(upstream))
    entry = draft.entry(pinned, {})

    assert entry.copyright_source is None


def test_a_notice_the_draft_does_not_declare_is_refused_and_shown(
    tmp_path: Path,
) -> None:
    apache = "Copyright 2024 Acme\nCopyright 2023 Contributors\n\n" + licence_text(
        "Apache-2.0"
    )
    upstream = {**FILES, "LICENSE": apache.encode()}

    with pytest.raises(CurationError) as refused:
        pin(draft_for(license="Apache-2.0"), tmp_path, fetch_writing(upstream))

    assert "Copyright 2024 Acme\nCopyright 2023 Contributors" in str(refused.value)


def test_a_draft_whose_licence_disagrees_with_upstream_s_is_refused(
    tmp_path: Path,
) -> None:
    upstream = {**FILES, "LICENSE": licence_text("CC-BY-4.0").encode()}

    with pytest.raises(CurationError, match=r"CC-BY-4\.0.*Apache-2\.0"):
        pin(draft_for(license="Apache-2.0"), tmp_path, fetch_writing(upstream))


def test_a_licence_file_readily_does_not_recognise_is_refused(tmp_path: Path) -> None:
    upstream = {**FILES, "LICENSE.md": b"# Acme Research Licence\nNo."}

    with pytest.raises(CurationError, match=r"LICENSE\.md"):
        pin(draft_for(), tmp_path, fetch_writing(upstream))


def test_a_notice_file_is_pinned_but_never_read_as_the_licence(tmp_path: Path) -> None:
    upstream = {**FILES, "NOTICE.txt": b"Includes work under MIT by others"}

    pinned = pin(draft_for(), tmp_path, fetch_writing(upstream))

    assert [file.path for file in pinned] == [*FILES, "NOTICE.txt"]


def test_upstream_s_notice_that_says_what_the_licence_prescribes_is_pinned(
    tmp_path: Path,
) -> None:
    prescribed = LICENCE_OBLIGATIONS["llama3.1"].notice
    upstream = {
        **FILES,
        "LICENSE": licence_text("llama3.1").encode(),
        "NOTICE": f"\n  {prescribed}  \n\n".encode(),
    }

    pinned = pin(draft_for(license="llama3.1"), tmp_path, fetch_writing(upstream))

    assert [file.path for file in pinned] == [*FILES, "LICENSE", "NOTICE"]
    make_entry(files=upstream, license="llama3.1")


def test_upstream_s_notice_that_says_anything_else_is_refused(tmp_path: Path) -> None:
    # The file would stand where the store writes the prescribed Notice,
    # so it has to be that Notice; a curator cannot declare it away.
    upstream = {
        **FILES,
        "LICENSE": licence_text("llama3.1").encode(),
        "Notice": b"Llama 3.1 is licensed under the Llama 3.1 Community License.",
    }

    with pytest.raises(CurationError) as refused:
        pin(draft_for(license="llama3.1"), tmp_path, fetch_writing(upstream))

    assert "Notice in " in str(refused.value)
    assert LICENCE_OBLIGATIONS["llama3.1"].notice in str(refused.value)


def test_a_notice_under_another_name_is_pinned_whatever_it_says(tmp_path: Path) -> None:
    upstream = {
        **FILES,
        "LICENSE": licence_text("llama3.1").encode(),
        "NOTICE.md": b"Vendored: tokenizer under MIT by others",
    }

    pinned = pin(draft_for(license="llama3.1"), tmp_path, fetch_writing(upstream))

    assert [file.path for file in pinned] == [*FILES, "LICENSE", "NOTICE.md"]


def test_a_notice_above_terms_that_leave_no_blank_is_declared_like_any_other(
    tmp_path: Path,
) -> None:
    # CC-BY names nobody in its text, yet upstream's copy can still open
    # with the holder's line; the entry it pins into must be able to
    # declare it, or the file can never be pinned.
    upstream = {
        **FILES,
        "LICENSE": f"{ACMES_NOTICE}\n\n{licence_text('CC-BY-4.0')}".encode(),
    }
    draft = draft_for(
        license="CC-BY-4.0",
        copyright_notice=ACMES_NOTICE,
        attribution={
            "creator": "Acme",
            "copyright_notice": ACMES_NOTICE,
            "modified": False,
        },
    )

    pinned = pin(draft, tmp_path, fetch_writing(upstream))

    assert [file.path for file in pinned] == [*FILES, "LICENSE"]
    make_entry(
        files=upstream,
        license="CC-BY-4.0",
        copyright_notice=ACMES_NOTICE,
        attribution={
            "creator": "Acme",
            "copyright_notice": ACMES_NOTICE,
            "modified": False,
        },
    )


def test_cc_by_attribution_does_not_have_to_appear_in_the_licence_file(
    tmp_path: Path,
) -> None:
    upstream = {**FILES, "LICENSE": licence_text("CC-BY-4.0").encode()}
    draft = draft_for(
        license="CC-BY-4.0",
        attribution={
            "creator": "Acme",
            "copyright_notice": ACMES_NOTICE,
            "modified": False,
        },
    )

    pinned = pin(draft, tmp_path, fetch_writing(upstream))

    assert [file.path for file in pinned] == [*FILES, "LICENSE"]


def test_only_a_file_named_as_a_licence_is_read_as_one(tmp_path: Path) -> None:
    upstream = {**FILES, "license_head.bin": b"\x00\x01", "onnx/LICENSE": b"No."}
    draft = Draft.from_entry(make_entry(files=upstream))

    with pytest.raises(CurationError, match=r"onnx/LICENSE"):
        pin(draft, tmp_path, fetch_writing(upstream))
