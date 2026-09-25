"""The licence travels with the weights (ADR 0009 §3): the app bundles every
allowlisted licence's text, recognises each by its own words, and knows
which files the store writes next to a download.
"""

import pytest
from conftest import FILES, make_entry

from readily_engine.catalog import LICENCE_OBLIGATIONS, MANIFEST_PATH
from readily_engine.catalog.licence_table import (
    LICENCE_FILE_NAMES,
    is_licence_file,
)
from readily_engine.catalog.licences import (
    LICENSES_DIR,
    files_the_store_writes,
    identify_licence,
    licence_text,
    read_licence,
)


def test_the_bundled_texts_live_beside_the_manifest():
    assert MANIFEST_PATH.parent / "licenses" == LICENSES_DIR


def test_every_allowlisted_licence_has_its_text_bundled_and_nothing_else_is():
    bundled = {path.stem for path in LICENSES_DIR.glob("*.txt")}

    assert bundled == set(LICENCE_OBLIGATIONS)
    assert all(licence_text(licence).strip() for licence in LICENCE_OBLIGATIONS)


@pytest.mark.parametrize("licence", sorted(LICENCE_OBLIGATIONS))
def test_no_bundled_text_hands_a_reader_a_placeholder(licence: str):
    # Apache's appendix is a template by design; nothing before its terms
    # end is one, no other text has a blank for a name or a year, and none
    # asserts some particular licensor's copyright over whatever it is
    # written beside.
    text = licence_text(licence).split("END OF TERMS AND CONDITIONS")[0]
    assert "<" not in text
    assert "[yyyy]" not in text
    assert "copyright (c)" not in text.casefold()


@pytest.mark.parametrize("licence", sorted(LICENCE_OBLIGATIONS))
def test_each_bundled_text_is_recognised_as_its_own_licence(licence: str):
    assert identify_licence(licence_text(licence)) == licence


def test_recognition_survives_the_ways_upstreams_reformat_a_licence():
    reflowed = "MIT License\n\nCopyright (c) 2024 Acme\n\n" + licence_text(
        "MIT"
    ).replace("\n", "\n\t").replace("  ", "\n")
    assert identify_licence(reflowed, "Copyright (c) 2024 Acme") == "MIT"
    markdown = "# " + licence_text("llama3.2").replace("\n\n", "\n\n**").replace(
        '"', "\u201c"
    )
    assert identify_licence(markdown) == "llama3.2"


@pytest.mark.parametrize("licence", ["llama3.1", "llama3.2", "llama3.3", "llama4"])
def test_a_llama_repo_ships_the_agreement_without_the_use_policy(licence: str):
    text = licence_text(licence)
    agreement, use_policy = text.split("arising out of this Agreement.")
    assert "Acceptable Use Policy" in use_policy

    assert identify_licence(agreement + "arising out of this Agreement.") == licence


def test_an_upstream_leaves_out_creative_commons_framing_around_its_licence():
    text = licence_text("CC-BY-4.0")
    start = text.index("Creative Commons Attribution 4.0 International Public License")
    end = text.index("Creative Commons is not a party to its public licenses")
    assert "not a law firm" in text[:start]

    assert identify_licence(text[start:end]) == "CC-BY-4.0"


def test_a_licensor_fills_the_blanks_a_licence_leaves_for_its_name():
    # What a blank holds is read back in the upstream's own punctuation and
    # case, and has to be what the curator declared, in the order the
    # licence leaves its blanks: a restriction written there is the same
    # letters as a name once squashed, and nobody declares one.
    bsd = (
        licence_text("BSD-3-Clause")
        .replace("the copyright holder", "Google Inc.")
        .replace("THE COPYRIGHT HOLDERS AND CONTRIBUTORS", "GOOGLE AND CONTRIBUTORS")
        .replace("THE COPYRIGHT HOLDER OR CONTRIBUTORS", "GOOGLE OR CONTRIBUTORS")
    )
    filled = "Google Inc.\nGOOGLE AND CONTRIBUTORS\nGOOGLE OR CONTRIBUTORS"
    assert read_licence(bsd) == ("BSD-3-Clause", filled)
    assert identify_licence(bsd, filled) == "BSD-3-Clause"
    assert identify_licence(bsd) is None
    assert identify_licence(bsd, "Google Inc.") is None
    mit = licence_text("MIT").replace("THE AUTHORS OR COPYRIGHT HOLDERS", "ACME")
    assert identify_licence(mit, "ACME") == "MIT"
    assert identify_licence(mit, "ACME.") is None
    assert identify_licence(mit, "Acme") is None


def test_a_blank_left_as_the_licence_wrote_it_is_not_a_notice():
    for licence in ("MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"):
        assert read_licence(licence_text(licence)) == (licence, None)
        assert identify_licence(licence_text(licence), "Acme") is None


def test_a_licence_is_its_whole_text_and_not_its_title():
    assert identify_licence("**LLAMA 3.2 COMMUNITY LICENSE AGREEMENT**\n...") is None
    assert identify_licence("CreativeML OpenRAIL-M\ndated August 22, 2022") is None
    assert identify_licence("Apache License\nVersion 2.0, January 2004") is None


def test_a_licence_with_a_term_added_or_removed_is_not_that_licence():
    apache = licence_text("Apache-2.0")
    narrowed = apache.replace(
        "2. Grant of Copyright License.",
        "2. Grant of Copyright License. Commercial use is prohibited.",
    )
    assert narrowed != apache
    assert identify_licence(narrowed) is None
    cut = apache.replace("7. Disclaimer of Warranty.", "")
    assert identify_licence(cut) is None
    assert identify_licence(licence_text("BSD-3-Clause").replace("3. Neither", "")) is (
        None
    )


def test_the_two_bsd_variants_are_told_apart():
    assert identify_licence(licence_text("BSD-2-Clause")) == "BSD-2-Clause"
    assert identify_licence(licence_text("BSD-3-Clause")) == "BSD-3-Clause"


def test_a_third_party_notice_appended_to_a_licence_does_not_win():
    combined = licence_text("llama3.1") + "\n\nThird-party:\n" + licence_text("MIT")
    assert identify_licence(combined) == "llama3.1"
    combined = licence_text("bigscience-openrail-m") + licence_text("BSD-3-Clause")
    assert identify_licence(combined) == "bigscience-openrail-m"
    combined = (
        "Copyright (c) 2021 Acme\n"
        + licence_text("BSD-3-Clause")
        + "\n\nThird-party notices\n\nCopyright (c) 2019 Someone Else (ONNX)\n"
        + "The MIT License\n"
        + licence_text("MIT")
    )
    notices = "Copyright (c) 2021 Acme\nCopyright (c) 2019 Someone Else (ONNX)"
    assert identify_licence(combined, notices) == "BSD-3-Clause"
    assert identify_licence(combined, "Copyright (c) 2021 Acme") is None
    combined = licence_text("MIT") + "\n\nLicenses:\n" + licence_text("BSD-2-Clause")
    assert identify_licence(combined) == "MIT"


RESTRICTION = "Commercial use is prohibited. Research use only."


@pytest.mark.parametrize("licence", ["MIT", "Apache-2.0", "CC-BY-4.0", "llama3.2"])
def test_a_restriction_written_around_a_licence_makes_it_another_licence(
    licence: str,
):
    # The file has to be the licence and nothing else: a term written above
    # or below the terms binds the reader just as one written into them.
    text = licence_text(licence)
    assert identify_licence(text) == licence
    assert identify_licence(f"{RESTRICTION}\n\n{text}") is None
    assert identify_licence(f"{text}\n\n{RESTRICTION}\n") is None
    notice = "Copyright (c) 2024 Acme."
    assert identify_licence(f"{notice} {RESTRICTION}\n{text}", notice) is None


def test_a_restriction_written_in_another_script_is_still_a_restriction():
    # Recognition squashes a file to its letters and digits, and a letter
    # is a letter in any script: a term written in Chinese around the
    # terms is text the file has to answer for, not punctuation to drop.
    text = licence_text("Apache-2.0")
    restriction = "仅限非商业用途"
    assert identify_licence(f"{text}\n{restriction}\n") is None
    assert identify_licence(f"{restriction}\n{text}") is None


def test_a_holder_s_name_keeps_its_diacritics_and_is_still_recognised():
    notice = "Copyright (c) 2024 Jürgen Müller"
    text = f"{notice}\n\n{licence_text('MIT')}"
    assert read_licence(text) == ("MIT", notice)
    assert identify_licence(text, notice) == "MIT"
    assert identify_licence(text, "Copyright (c) 2024 Jurgen Muller") is None


def test_what_may_stand_beside_the_terms_is_the_licensor_s_own_notice():
    # A title naming the licence, the licensor's copyright line, a heading
    # over appended third-party notices, and separators are not terms.
    text = licence_text("MIT")
    notice = (
        "Copyright (c) 2019-2024 Acme Inc. All rights reserved.\n"
        "Portions Copyright 2018 Someone Else"
    )
    framed = (
        "# The MIT License (MIT)\n\n"
        f"{notice}\n"
        "----------------------------------------\n\n"
        f"{text}\n"
    )
    assert identify_licence(framed, notice) == "MIT"
    assert identify_licence(framed) is None
    assert identify_licence(f"MIT License - Non-Commercial\n\n{text}") is None
    assert identify_licence(f"{text}\n\nThe following applies to onnx/:\n") is None


def test_the_notice_beside_the_terms_is_exactly_what_the_curator_declared():
    # What stands beside the terms is checked against a reviewed expectation
    # and nothing weaker: a line that is not the declared notice is a term,
    # whatever it says, and a declared notice the file lacks is missing.
    text = licence_text("MIT")
    notice = "Copyright (c) 2024 Acme Inc.\nAll rights reserved."
    assert identify_licence(f"{notice}\n\n{text}", notice) == "MIT"
    assert identify_licence(f"{notice}\n\n{text}") is None
    assert identify_licence(text, notice) is None
    assert identify_licence(f"Copyright (c) 2024 Acme\n\n{text}", notice) is None
    assert (
        identify_licence(f"{notice}\nNon-commercial use only.\n{text}", notice) is None
    )
    assert identify_licence(f"{text}\n\n{notice}\n", notice) == "MIT"
    assert identify_licence(f"{notice}\n{text}\n{notice}\n", notice) is None
    assert identify_licence(f"{text}\n\nAll rights reserved.\n", notice) is None


def test_the_notice_is_compared_line_by_line_and_nothing_looser():
    # Newlines, blank lines and the whitespace around a line are how a file
    # is laid out, not what it says; every other character is what it says.
    text = licence_text("MIT")
    notice = "Copyright (c) 2024 Acme Inc.\nAll rights reserved."
    for written in (
        "Copyright (c) 2024 Acme Inc.\r\nAll rights reserved.\r\n",
        "  Copyright (c) 2024 Acme Inc.  \n\n\n  All rights reserved.",
    ):
        assert identify_licence(f"{written}\n{text}", notice) == "MIT", written
        assert read_licence(f"{written}\n{text}") == ("MIT", notice)
    assert identify_licence(f"{notice}\n\n{text}", f"\n{notice}\n\n") == "MIT"
    assert identify_licence(f"{notice}\n\n{text}", notice.lower()) is None
    assert identify_licence(f"{notice}\n\n{text}", notice.replace(".", "")) is None
    assert identify_licence(f"{notice}\n\n{text}", notice.replace("\n", " ")) is None


def test_what_a_file_says_where_the_notice_sits_is_read_back_for_the_curator():
    # A refusal has to show the curator the lines to copy into the draft,
    # from the licensor's copyright line to a vendored component's, in the
    # order the file writes them.
    text = licence_text("MIT")
    assert read_licence(text) == ("MIT", None)
    assert read_licence("MIT License\n" + text) == ("MIT", None)
    assert read_licence(f"Copyright (c) 2024 Acme\n\n{text}") == (
        "MIT",
        "Copyright (c) 2024 Acme",
    )
    combined = (
        f"Copyright (c) 2021 Acme\n{licence_text('BSD-3-Clause')}"
        f"\n\nThird-party notices\nCopyright (c) 2019 Other\n{text}"
    )
    assert read_licence(combined) == (
        "BSD-3-Clause",
        "Copyright (c) 2021 Acme\nCopyright (c) 2019 Other",
    )
    assert read_licence("Copyright (c) 2024 Acme\nNot a licence") == (None, None)
    assert read_licence("") == (None, None)


def test_apache_s_appendix_may_be_filled_in_as_it_asks():
    text = licence_text("Apache-2.0")
    blank = "[yyyy] [name of copyright owner]"
    filled = text.replace(blank, "2023 Acme Inc.")
    assert identify_licence(filled, "2023 Acme Inc.") == "Apache-2.0"
    assert identify_licence(filled) is None
    assert identify_licence(text.split("APPENDIX")[0]) == "Apache-2.0"
    above = f"Copyright 2023 Acme Inc.\n\n{filled}"
    assert read_licence(above) == (
        "Apache-2.0",
        "Copyright 2023 Acme Inc.\n2023 Acme Inc.",
    )


def test_a_file_that_quotes_two_binding_agreements_is_neither():
    combined = licence_text("llama3.1") + "\n\n" + licence_text("llama3.2")
    assert identify_licence(combined) is None


def test_an_agreement_that_binds_the_reader_is_the_licence_wherever_it_sits():
    # A repo whose LICENSE opens with a vendored component's MIT block and
    # carries its own Llama agreement below it is under Llama, and a draft
    # declaring MIT for it would ship the weights with no Notice.
    combined = licence_text("MIT") + "\n\n" + licence_text("llama3.2")
    assert identify_licence(combined) == "llama3.2"
    combined = licence_text("BSD-3-Clause") + "\n\n" + licence_text("llama4")
    assert identify_licence(combined) == "llama4"


def test_a_text_that_is_no_allowlisted_licence_is_not_guessed_at():
    assert identify_licence("Attribution-NonCommercial 4.0 International") is None
    assert identify_licence("") is None


def test_the_candidate_file_names_are_the_ones_upstreams_use():
    # Hugging Face paths are case-sensitive, and upstreams write these in
    # every case, so each spelling is its own exact name.
    assert LICENCE_FILE_NAMES == (
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "License",
        "License.md",
        "License.txt",
        "license",
        "license.md",
        "license.txt",
        "NOTICE",
        "NOTICE.md",
        "NOTICE.txt",
        "Notice",
        "Notice.md",
        "Notice.txt",
        "notice",
        "notice.md",
        "notice.txt",
    )


def test_a_licence_file_is_known_by_its_name_wherever_it_sits():
    assert is_licence_file("LICENSE")
    assert is_licence_file("license.txt")
    assert is_licence_file("onnx/LICENSE.md")
    assert not is_licence_file("NOTICE")
    assert not is_licence_file("license_head.bin")


class TestFilesTheStoreWrites:
    def test_an_entry_without_upstream_s_licence_file_gets_the_bundled_copy(self):
        written = files_the_store_writes(make_entry(FILES, license="Apache-2.0"))

        assert written == {"LICENSE": licence_text("Apache-2.0")}

    @pytest.mark.parametrize("licence", ["MIT", "BSD-2-Clause", "BSD-3-Clause"])
    def test_a_notice_without_upstream_s_file_gets_its_holder_line_written(
        self, licence: str
    ):
        notice = "Copyright (c) 2024 Acme"
        entry = make_entry(
            FILES,
            license=licence,
            copyright_notice=notice,
            copyright_source="https://example.com/acme/LICENSE",
        )

        written = files_the_store_writes(entry)["LICENSE"]

        assert written.startswith(
            f"{LICENCE_OBLIGATIONS[licence].display_name} License\n\n{notice}\n\n"
        )
        assert identify_licence(written, notice) == licence

    def test_upstream_s_own_licence_file_is_not_shadowed(self):
        entry = make_entry(
            {**FILES, "LICENSE.txt": b"MIT..."}, license="MIT", copyright_notice="Acme"
        )

        assert files_the_store_writes(entry) == {}

    def test_a_licence_with_a_prescribed_notice_gets_it_written(self):
        entry = make_entry(FILES, license="llama3.1")

        written = files_the_store_writes(entry)

        assert written["Notice"] == LICENCE_OBLIGATIONS["llama3.1"].notice
        assert written["LICENSE"] == licence_text("llama3.1")

    def test_a_licence_with_no_prescribed_notice_gets_none(self):
        written = files_the_store_writes(make_entry(FILES, license="CC0-1.0"))

        assert "Notice" not in written

    def test_upstream_s_own_notice_file_is_the_one_that_ships(self):
        # Llama §1.b.iii asks every distributor for the same `Notice`, so a
        # compliant upstream ships it already; pinned, it is the hash-verified
        # copy and the store's own would only overwrite it.
        prescribed = LICENCE_OBLIGATIONS["llama3.1"].notice.encode()
        entry = make_entry({**FILES, "NOTICE": prescribed}, license="llama3.1")

        assert "Notice" not in files_the_store_writes(entry)

    def test_a_notice_under_another_name_does_not_stand_in_for_the_prescribed_one(
        self,
    ):
        entry = make_entry({**FILES, "NOTICE.md": b"Vendored: ..."}, license="llama3.1")

        prescribed = LICENCE_OBLIGATIONS["llama3.1"].notice
        assert files_the_store_writes(entry)["Notice"] == prescribed
