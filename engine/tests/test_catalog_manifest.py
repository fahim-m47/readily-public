"""The Catalog Manifest is curation data that CI has to be able to reject.

The manifest is baked into the release (ADR 0003 §1), so a bad entry cannot
be fixed by a server-side edit — it ships. These tests are the gate: the
committed manifest parses, and every field a curation PR could get subtly
wrong is refused rather than carried into a build.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

# The one complete valid entry lives in conftest, so a new required field on
# `CatalogEntry` is added in one place rather than in every fixture literal.
from conftest import ENTRY, MANIFEST, entry_dict
from pydantic import ValidationError

from readily_engine.audio.artifacts import frame_rms, read_wav
from readily_engine.catalog import (
    LICENCE_OBLIGATIONS,
    MANIFEST_PATH,
    REFERENCE_ROOT,
    Manifest,
    Source,
    Tier,
    load_manifest,
)
from readily_engine.catalog.recipes import qualified, recipe_digest
from readily_engine.generation import GenerationRecord
from readily_engine.loading.qwen3 import CODEC_TOKENS_PER_SECOND
from readily_engine.loading.references import REFERENCE_SAMPLE_RATE, voice_references
from readily_engine.server.entries import default_here


def manifest(**overrides) -> Manifest:
    """Validate MANIFEST with the given top-level keys replaced."""
    return Manifest.model_validate({**copy.deepcopy(MANIFEST), **overrides})


def one_model(**overrides) -> Manifest:
    """Validate a manifest whose single entry carries the given overrides."""
    return manifest(models=[entry_dict(**overrides)])


def attribution() -> dict[str, object]:
    return {
        "creator": "Acme Audio",
        "copyright_notice": "Copyright 2026 Acme Audio",
        "modified": False,
    }


def attribution_if_needed(licence: str) -> dict[str, object]:
    if LICENCE_OBLIGATIONS[licence].requires_attribution:
        return {"attribution": attribution()}
    return {"attribution": None}


def test_the_committed_manifest_validates():
    catalog = load_manifest()

    assert catalog.schema_version == 1
    assert {entry.id: entry.architecture for entry in catalog.models} == {
        "kokoro:82m": "kokoro",
        "qwen3-tts:0.6b": "qwen3",
        "qwen3-tts:1.7b": "qwen3",
        "kitten-tts:15m": "kitten",
        "chatterbox:turbo": "chatterbox_turbo",
        "supertonic:66m": "supertonic",
        "supertonic:99m": "supertonic",
        "vibevoice-realtime:0.5b": "vibevoice",
        "voxcpm2:2b": "voxcpm2",
    }


def test_qwen_uses_non_streaming_decode_and_invalidates_old_segments():
    entry = load_manifest().find("qwen3-tts:0.6b")
    assert entry.decode_mode == "non-streaming"
    assert entry.version == 5


def test_decode_mode_survives_recuration():
    from readily_engine.curation import Draft

    entry = one_model(decode_mode="non-streaming").models[0]
    draft = Draft.from_entry(entry)
    assert draft.entry(entry.files, {}).decode_mode == "non-streaming"


def test_an_unknown_decode_mode_is_refused():
    with pytest.raises(ValidationError):
        one_model(decode_mode="nonstreaming")


def test_the_committed_manifest_ships_both_tiers():
    catalog = load_manifest()
    by_id = {entry.id: entry for entry in catalog.models}

    assert by_id["kokoro:82m"].tier is Tier.INSTANT
    assert by_id["qwen3-tts:0.6b"].tier is Tier.EXPRESSIVE
    assert by_id["chatterbox:turbo"].tier is Tier.EXPRESSIVE


def test_the_committed_manifest_lives_where_the_build_bakes_it_in():
    # ADR 0003 §1: catalog/manifest.json, bundled at build. The path is part
    # of the contract with whatever packages the app, not an Engine detail.
    assert MANIFEST_PATH.name == "manifest.json"
    assert MANIFEST_PATH.parent.name == "catalog"
    assert MANIFEST_PATH.is_file()


def test_a_source_page_names_the_pinned_hugging_face_revision():
    source = Source(hf_repo="acme/model", revision="a" * 40)

    assert source.page_url == f"https://huggingface.co/acme/model/tree/{'a' * 40}"


@pytest.mark.parametrize(
    "hand_edited",
    [
        "a" * 63,  # truncated paste
        "a" * 65,  # a stray character
        "A" * 64,  # shasum on a platform that upper-cases
        "sha256:" + "a" * 64,  # prefixed, as some tools print it
        "g" * 64,  # not hex at all
        "",
    ],
    ids=["short", "long", "uppercase", "prefixed", "not-hex", "empty"],
)
def test_a_hand_edited_hash_fails_validation(hand_edited: str):
    with pytest.raises(ValidationError):
        one_model(files=[{"path": "m.onnx", "sha256": hand_edited, "size_bytes": 12}])


def test_a_file_needs_a_hash_at_all():
    with pytest.raises(ValidationError):
        one_model(files=[{"path": "m.onnx", "size_bytes": 12}])


def test_an_entry_with_no_files_is_refused():
    # Nothing to verify means nothing is verified; the promote invariant
    # would be vacuously true (ADR 0003 §3).
    with pytest.raises(ValidationError):
        one_model(files=[])


def test_duplicate_file_paths_are_refused():
    with pytest.raises(ValidationError):
        one_model(
            files=[
                {"path": "m.onnx", "sha256": "a" * 64, "size_bytes": 12},
                {"path": "m.onnx", "sha256": "b" * 64, "size_bytes": 12},
            ]
        )


def test_a_derived_file_must_come_from_a_pinned_one():
    with pytest.raises(ValidationError, match="not pinned"):
        one_model(
            derived_files=[
                {"path": "m.timed.onnx", "source": "other.onnx", "outputs": ["d"]}
            ]
        )


def test_a_derived_file_cannot_shadow_a_pinned_one():
    with pytest.raises(ValidationError, match=r"m\.onnx"):
        one_model(
            files=[
                {"path": "m.onnx", "sha256": "a" * 64, "size_bytes": 12},
                {"path": "v.bin", "sha256": "b" * 64, "size_bytes": 12},
            ],
            derived_files=[{"path": "m.onnx", "source": "v.bin", "outputs": ["d"]}],
        )


def test_file_paths_that_only_case_tells_apart_are_refused():
    # These land in one model directory, and on a case-insensitive volume
    # two case-variant paths are one file: the second download overwrites
    # the first, and verification refuses whichever hash lost.
    with pytest.raises(ValidationError, match="case-insensitive"):
        one_model(
            files=[
                {"path": "M.onnx", "sha256": "a" * 64, "size_bytes": 12},
                {"path": "m.onnx", "sha256": "b" * 64, "size_bytes": 12},
            ]
        )


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../escape.bin", "voices/../../escape.bin", ""]
)
def test_a_file_path_that_escapes_the_model_directory_is_refused(path: str):
    # The store joins these onto the model directory. A path that climbs out
    # of it writes wherever it likes, verified hash and all (B1).
    with pytest.raises(ValidationError):
        one_model(files=[{"path": path, "sha256": "a" * 64, "size_bytes": 12}])


@pytest.mark.parametrize(
    "path", ["*.onnx", "voices/?.bin", "voices/[ab].bin"], ids=["star", "qm", "set"]
)
def test_a_file_path_with_wildcard_characters_is_refused(path: str):
    # The download passes these paths as huggingface_hub allow-patterns,
    # which are fnmatch globs. "Exactly the files the manifest names" (B1)
    # only holds if a path cannot also be a pattern.
    with pytest.raises(ValidationError):
        one_model(files=[{"path": path, "sha256": "a" * 64, "size_bytes": 12}])


@pytest.mark.parametrize(
    "path", [".broken", ".broken/weights.bin", ".Broken", ".BROKEN/weights.bin"]
)
def test_a_file_path_named_like_the_broken_marker_is_refused(path: str):
    # `store/` reads `.broken` at the top of a promoted directory as "repair
    # found this model corrupt and could not retire it". An artifact of that
    # name would make a verified model look broken the moment it was
    # promoted, and the next download would retire it. Any spelling: the
    # store reads the marker off a volume that ignores case.
    with pytest.raises(ValidationError):
        one_model(files=[{"path": path, "sha256": "a" * 64, "size_bytes": 12}])


@pytest.mark.parametrize(
    "revision",
    ["main", "v1.0", "0" * 39, "0" * 41, "Z" * 40],
    ids=["branch", "tag", "short", "long", "not-hex"],
)
def test_a_mutable_revision_is_refused(revision: str):
    # ADR 0003 §2: the repo is pinned to an immutable revision commit. A
    # branch name is exactly the pin a compromised upstream can move.
    with pytest.raises(ValidationError):
        one_model(source={"hf_repo": "acme/kokoro", "revision": revision})


@pytest.mark.parametrize("repo", ["kokoro", "acme/kokoro/extra", ""])
def test_a_repo_that_is_not_owner_slash_name_is_refused(repo: str):
    with pytest.raises(ValidationError):
        one_model(source={"hf_repo": repo, "revision": "0" * 40})


def test_identity_is_name_colon_tag():
    assert one_model().models[0].id == "acme:1m"


def test_a_bare_name_resolves_to_the_default_tag():
    catalog = load_manifest()

    assert catalog.resolve("kokoro") is catalog.find("kokoro:82m")
    assert catalog.resolve("qwen3-tts") is catalog.find("qwen3-tts:0.6b")


def test_a_fully_qualified_reference_resolves_to_itself():
    catalog = load_manifest()

    assert catalog.resolve("kokoro:82m") is catalog.find("kokoro:82m")


def test_an_unknown_reference_resolves_to_nothing():
    catalog = load_manifest()

    assert catalog.resolve("kokoro:99b") is None
    assert catalog.resolve("llama") is None
    assert catalog.resolve("") is None


def test_duplicate_entry_ids_are_refused():
    with pytest.raises(ValidationError):
        manifest(models=[entry_dict(), entry_dict()])


def test_the_fresh_install_default_is_named_not_positional():
    # A curation PR that reorders `models` must not silently change what a
    # fresh install narrates with.
    catalog = load_manifest()

    assert catalog.default_model == "vibevoice-realtime"
    assert catalog.default_entry.id == "vibevoice-realtime:0.5b"
    assert catalog.default_entry.default_voice == "en-Mike_man"


def test_the_escape_from_a_wait_is_named_not_positional():
    catalog = load_manifest()

    assert catalog.default_fast_model == "supertonic:99m"
    assert catalog.default_fast_entry is not None
    assert catalog.default_fast_entry.id == "supertonic:99m"
    assert catalog.default_fast_entry.tier == "instant"
    assert catalog.default_fast_entry.default_voice == "M3"


def test_a_default_model_that_resolves_to_nothing_is_refused():
    with pytest.raises(ValidationError):
        manifest(default_model="llama")


def test_a_default_fast_model_that_resolves_to_nothing_is_refused():
    with pytest.raises(ValidationError):
        manifest(default_fast_model="llama")


def test_a_manifest_may_name_no_fast_model_at_all():
    assert manifest().default_fast_entry is None
    assert manifest(default_fast_model="acme:1m").default_fast_entry is not None


def test_a_machine_that_runs_neither_default_is_told_the_manifests():
    # Nothing to fall back to: the answer stays the Manifest's default, and
    # the download of it says `model_unsupported` rather than the Engine
    # inventing a third choice.
    shipped = load_manifest()

    assert default_here(shipped, frozenset()) is shipped.default_entry
    assert default_here(shipped, frozenset({"onnxruntime"})) is (
        shipped.default_fast_entry
    )
    assert default_here(shipped, frozenset({"onnxruntime", "mlx-audio"})) is (
        shipped.default_entry
    )


def test_the_default_model_may_be_a_fully_qualified_reference():
    assert manifest(default_model="acme:1m").default_entry.id == "acme:1m"


def test_a_manifest_without_a_default_model_is_refused():
    incomplete = copy.deepcopy(MANIFEST)
    del incomplete["default_model"]

    with pytest.raises(ValidationError):
        Manifest.model_validate(incomplete)


def test_a_name_with_no_default_tag_is_refused():
    # A bare name must always resolve, so every name owns a default tag.
    with pytest.raises(ValidationError):
        one_model(default_tag=False)


def test_a_name_with_two_default_tags_is_refused():
    first = entry_dict()
    second = entry_dict(tag="1m-int8")

    with pytest.raises(ValidationError):
        manifest(models=[first, second])


def test_two_tags_of_one_name_are_allowed_with_a_single_default():
    second = entry_dict(tag="1m-int8", default_tag=False)

    catalog = manifest(models=[entry_dict(), second])

    assert catalog.resolve("acme").id == "acme:1m"


@pytest.mark.parametrize("name", ["Kokoro", "kokoro:82m", "kokoro 82m", ""])
def test_a_name_that_would_break_name_colon_tag_parsing_is_refused(name: str):
    with pytest.raises(ValidationError):
        one_model(name=name)


@pytest.mark.parametrize("tag", ["82M", "82m:x", "82 m", ""])
def test_a_tag_that_would_break_name_colon_tag_parsing_is_refused(tag: str):
    with pytest.raises(ValidationError):
        one_model(tag=tag)


def test_a_version_that_is_not_an_incrementable_counter_is_refused():
    # ADR 0003 §5: weight bumps increment `version`; Segment-cache keys
    # include it, so it has to order.
    with pytest.raises(ValidationError):
        one_model(version="1.0")


def test_an_unknown_tier_is_refused():
    # Tier is the pill the picker renders: user-facing vocabulary, frozen
    # here rather than free text a curation PR can invent.
    with pytest.raises(ValidationError):
        one_model(tier="blazing")


@pytest.mark.parametrize("architecture", ["", "Kokoro", "kokoro-82m", "3d", "a b"])
def test_an_architecture_id_the_registry_could_never_name_is_refused(architecture):
    # The parser cannot ask the registry (`catalog/` imports nothing from
    # `loading/`), so it holds the id to the registry's spelling and leaves
    # "is it registered?" to `readily-curate --write` and Engine boot.
    with pytest.raises(ValidationError):
        one_model(architecture=architecture)


@pytest.mark.parametrize(
    "licence",
    [
        # ADR 0009 §1: a scope narrower than "personal, on your own machine"
        # is one Readily cannot promise a reader their narration is theirs.
        "CC-BY-NC-4.0",
        "mistral-research",
        # Copyleft weights stay out until an ADR says what they owe.
        "GPL-3.0",
        # Unversioned and ambiguous: not the id either OpenRAIL-M row uses.
        "openrail",
        "Apache 2.0",
    ],
)
def test_a_model_licence_readily_may_not_hand_a_reader_is_refused(licence: str):
    with pytest.raises(ValidationError, match="redistributable licence allowlist"):
        one_model(license=licence)


def test_the_refusal_names_the_allowlist():
    with pytest.raises(ValidationError, match=r"CC-BY-4\.0.*llama3\.1.*llama4"):
        one_model(license="CC-BY-NC-4.0")


@pytest.mark.parametrize("licence", sorted(LICENCE_OBLIGATIONS))
def test_every_licence_on_the_list_is_one_an_entry_may_carry(licence: str):
    files = [
        {"path": "m.onnx", "sha256": "a" * 64, "size_bytes": 12},
        {"path": "LICENSE", "sha256": "b" * 64, "size_bytes": 12},
    ]
    entry = one_model(
        license=licence,
        files=files,
        copyright_notice="Acme",
        **attribution_if_needed(licence),
    ).models[0]
    assert entry.license == licence


@pytest.mark.parametrize("licence", ["MIT", "BSD-2-Clause", "BSD-3-Clause"])
def test_a_notice_without_an_upstream_file_records_where_its_holder_line_came_from(
    licence: str,
):
    with pytest.raises(ValidationError, match="copyright_source"):
        one_model(license=licence, copyright_notice="Acme")
    one_model(
        license=licence,
        copyright_notice="Acme",
        copyright_source="https://example.com/acme/LICENSE",
    )


@pytest.mark.parametrize("licence", ["MIT", "BSD-2-Clause", "BSD-3-Clause"])
def test_a_pinned_notice_file_is_the_source_instead_of_a_declared_url(licence: str):
    files = [
        {"path": "m.onnx", "sha256": "a" * 64, "size_bytes": 12},
        {"path": "onnx/LICENSE.txt", "sha256": "b" * 64, "size_bytes": 12},
    ]
    one_model(license=licence, copyright_notice="Acme", files=files)
    with pytest.raises(ValidationError, match="copyright_source"):
        one_model(
            license=licence,
            copyright_notice="Acme",
            copyright_source="https://example.com/acme/LICENSE",
            files=files,
        )


def test_only_a_notice_without_an_upstream_file_has_a_copyright_source():
    with pytest.raises(ValidationError, match="copyright_source"):
        one_model(
            license="Apache-2.0",
            copyright_source="https://example.com/acme/LICENSE",
        )


def test_a_copyright_source_is_an_http_url():
    with pytest.raises(ValidationError, match="copyright_source"):
        one_model(
            license="MIT",
            copyright_notice="Acme",
            copyright_source="not a URL",
        )

    entry = one_model(
        license="MIT",
        copyright_notice="Acme",
        copyright_source="https://example.com/acme/LICENSE",
    ).models[0]
    assert str(entry.copyright_source) == "https://example.com/acme/LICENSE"


LICENCE_FILE = {"path": "LICENSE", "sha256": "b" * 64, "size_bytes": 12}


@pytest.mark.parametrize("licence", ["MIT", "BSD-2-Clause", "BSD-3-Clause"])
def test_a_licence_that_is_a_notice_declares_whose_notice_it_is(licence: str):
    # The bundled text names nobody, so the entry says what upstream's file
    # writes where the licence leaves room for the holder, and curation
    # checks the file says exactly that (ADR 0009 §3).
    files = [ENTRY["files"][0], LICENCE_FILE]
    with pytest.raises(ValidationError, match="copyright_notice"):
        one_model(license=licence, files=files, copyright_notice=None)
    one_model(license=licence, files=files, copyright_notice="Copyright (c) Acme")


@pytest.mark.parametrize("licence", ["Apache-2.0", "llama3.2", "bigscience-openrail-m"])
def test_a_notice_is_what_upstream_s_file_says_beside_any_licence_s_terms(
    licence: str,
):
    # Whether or not the text leaves a blank for the licensor, upstream's copy
    # may open with the holder's line, and curation holds the file to the
    # declaration (ADR 0009 §3); so the schema admits one under every licence.
    files = [ENTRY["files"][0], LICENCE_FILE]
    one_model(
        license=licence,
        files=files,
        copyright_notice="Copyright 2024 Acme",
        **attribution_if_needed(licence),
    )
    one_model(
        license=licence,
        files=files,
        copyright_notice=None,
        **attribution_if_needed(licence),
    )


def test_a_declared_notice_is_not_blank():
    files = [ENTRY["files"][0], LICENCE_FILE]
    with pytest.raises(ValidationError, match="copyright_notice"):
        one_model(license="MIT", files=files, copyright_notice="")


def test_a_licence_that_asks_for_attribution_must_declare_it():
    with pytest.raises(ValidationError, match="reader-facing attribution"):
        one_model(license="CC-BY-4.0")

    entry = one_model(
        license="CC-BY-4.0",
        attribution=attribution(),
    ).models[0]

    assert entry.attribution is not None
    assert entry.attribution.creator == "Acme Audio"


def test_a_licence_that_asks_for_no_attribution_refuses_it():
    with pytest.raises(ValidationError, match="attribution must be null"):
        one_model(license="Apache-2.0", attribution=attribution())


def test_reader_facing_attribution_facts_are_not_blank():
    with pytest.raises(ValidationError):
        one_model(
            license="CC-BY-4.0",
            attribution={**attribution(), "creator": ""},
        )
    with pytest.raises(ValidationError, match="copyright_notice"):
        one_model(
            license="CC-BY-4.0",
            attribution={**attribution(), "copyright_notice": ""},
        )


def test_reader_facing_attribution_requires_a_copyright_notice():
    without_notice = attribution()
    del without_notice["copyright_notice"]

    with pytest.raises(ValidationError, match="copyright_notice"):
        one_model(license="CC-BY-4.0", attribution=without_notice)


def test_reader_facing_attribution_records_when_readily_modified_the_files():
    entry = one_model(
        license="CC-BY-4.0",
        attribution={**attribution(), "modified": True},
    ).models[0]

    assert entry.attribution is not None
    assert entry.attribution.modified is True


def test_reader_facing_attribution_requires_an_explicit_boolean():
    with pytest.raises(ValidationError):
        one_model(
            license="CC-BY-4.0",
            attribution={**attribution(), "modified": "false"},
        )


def test_only_cc_by_requires_reader_facing_attribution():
    requiring = {
        licence
        for licence, obligations in LICENCE_OBLIGATIONS.items()
        if obligations.requires_attribution
    }
    assert requiring == {"CC-BY-4.0"}
    assert {
        licence
        for licence, obligations in LICENCE_OBLIGATIONS.items()
        if obligations.reader_attribution is not None
    } == requiring


def test_the_allowlist_is_exactly_what_the_adr_admits():
    # ADR 0009 §2 makes this list the whole of the policy and says it moves
    # only by an ADR amending it. Asserting committed entries against the
    # list cannot enforce that — `load_manifest` has already run the validator
    # by the time the assertion reads them, so it passes for any list at all,
    # widened included. Spelling the members out is what makes a widening a
    # visible edit to a test rather than a silent one to a frozenset.
    assert frozenset(
        {
            "MIT",
            "BSD-2-Clause",
            "BSD-3-Clause",
            "CC0-1.0",
            "Apache-2.0",
            "CC-BY-4.0",
            "llama3.1",
            "llama3.2",
            "llama3.3",
            "llama4",
            "bigscience-openrail-m",
            "creativeml-openrail-m",
        }
    ) == frozenset(LICENCE_OBLIGATIONS)


@pytest.mark.parametrize(
    ("licence", "display_name"),
    [
        ("MIT", "MIT"),
        ("BSD-2-Clause", "BSD 2-Clause"),
        ("BSD-3-Clause", "BSD 3-Clause"),
        ("CC0-1.0", "CC0 1.0"),
        ("Apache-2.0", "Apache 2.0"),
        ("CC-BY-4.0", "Creative Commons Attribution 4.0"),
        ("llama3.1", "Llama 3.1 Community License"),
        ("llama3.2", "Llama 3.2 Community License"),
        ("llama3.3", "Llama 3.3 Community License"),
        ("llama4", "Llama 4 Community License"),
        ("bigscience-openrail-m", "BigScience OpenRAIL-M"),
        ("creativeml-openrail-m", "CreativeML OpenRAIL-M"),
    ],
)
def test_the_sheet_names_each_licence_the_way_its_own_text_does(
    licence: str, display_name: str
):
    assert LICENCE_OBLIGATIONS[licence].display_name == display_name


def test_the_licences_that_bind_the_reader_are_the_ones_the_adr_says_do():
    # ADR 0009 §2's last column. The sheet reads this to print "downloading
    # accepts …", so a row flipped by hand is a sheet that lies.
    binding = {id for id, o in LICENCE_OBLIGATIONS.items() if o.binds_reader}
    assert binding == {
        "llama3.1",
        "llama3.2",
        "llama3.3",
        "llama4",
        "bigscience-openrail-m",
        "creativeml-openrail-m",
    }


@pytest.mark.parametrize(
    ("licence", "notice"),
    [
        (
            "llama3.1",
            "Llama 3.1 is licensed under the Llama 3.1 Community License, "
            "Copyright © Meta Platforms, Inc. All Rights Reserved.",
        ),
        (
            "llama3.2",
            "Llama 3.2 is licensed under the Llama 3.2 Community License, "
            "Copyright © Meta Platforms, Inc. All Rights Reserved.",
        ),
        (
            "llama3.3",
            "Llama 3.3 is licensed under the Llama 3.3 Community License, "
            "Copyright © Meta Platforms, Inc. All Rights Reserved.",
        ),
        (
            "llama4",
            "Llama 4 is licensed under the Llama 4 Community License, "
            "Copyright © Meta Platforms, Inc. All Rights Reserved.",
        ),
    ],
)
def test_the_notice_text_is_the_licence_s_own_words(licence: str, notice: str):
    # The store writes these bytes next to the weights (Llama §1.b.iii). The
    # licence prescribes the string; a paraphrase is not a Notice.
    assert LICENCE_OBLIGATIONS[licence].notice == notice


def test_only_llama_asks_for_a_ui_attribution_line():
    with_attribution = {
        id for id, o in LICENCE_OBLIGATIONS.items() if o.credit is not None
    }
    assert with_attribution == {"llama3.1", "llama3.2", "llama3.3", "llama4"}


def test_only_llama_prescribes_a_notice_file():
    with_notice = {id for id, o in LICENCE_OBLIGATIONS.items() if o.notice is not None}
    assert with_notice == {"llama3.1", "llama3.2", "llama3.3", "llama4"}


def test_every_committed_entry_ships_under_the_licence_it_was_curated_for():
    # A licence read back by name, because an entry can be re-pinned to a
    # different upstream revision — or a different repo — without its id
    # changing, and the licence is the field that quietly stops being true
    # when that happens.
    licences = {entry.id: entry.license for entry in load_manifest().models}

    assert licences == {
        "kokoro:82m": "Apache-2.0",
        "qwen3-tts:0.6b": "Apache-2.0",
        "qwen3-tts:1.7b": "Apache-2.0",
        "kitten-tts:15m": "Apache-2.0",
        "chatterbox:turbo": "MIT",
        "supertonic:66m": "bigscience-openrail-m",
        "supertonic:99m": "bigscience-openrail-m",
        "vibevoice-realtime:0.5b": "MIT",
        "voxcpm2:2b": "Apache-2.0",
    }


def test_an_unknown_field_is_refused():
    # A misspelled tunable that is silently ignored is a tunable that does
    # nothing, and nothing in a baked manifest reports it.
    with pytest.raises(ValidationError):
        one_model(chunk_budget_chars=450)


def test_a_missing_provenance_note_is_refused():
    # B1 makes the curation PR the review point; the reviewer needs to know
    # where these bytes came from without re-deriving it.
    entry = entry_dict()
    del entry["provenance"]

    with pytest.raises(ValidationError):
        manifest(models=[entry])


def test_a_default_voice_the_entry_does_not_offer_is_refused():
    with pytest.raises(ValidationError):
        one_model(default_voice="am_michael")


def test_duplicate_voice_ids_are_refused():
    voice = {"id": "af_heart", "name": "Heart", "language": "en-US"}

    with pytest.raises(ValidationError):
        one_model(voices=[voice, dict(voice, name="Heart II")])


def test_voice_ids_that_only_case_tells_apart_are_refused():
    # A Voice id spells the clip and capture filenames, and APFS's default
    # volumes are case-insensitive: `Chelsie` and `chelsie` would silently
    # overwrite each other's files.
    voices = [
        {"id": "Chelsie", "name": "Chelsie", "language": "en-US"},
        {"id": "chelsie", "name": "Chelsie II", "language": "en-US"},
    ]

    with pytest.raises(ValidationError, match="case-insensitive"):
        one_model(voices=voices, default_voice="Chelsie")


def test_an_entry_with_no_voices_is_refused():
    # A Voice Model that offers no Voice cannot be chosen in the picker.
    with pytest.raises(ValidationError):
        one_model(voices=[], default_voice="af_heart")


def test_every_committed_voice_is_reachable_and_named():
    for entry in load_manifest().models:
        assert entry.voices, entry.id
        assert entry.default_voice in {voice.id for voice in entry.voices}


def test_kokoro_voice_ids_and_languages_name_the_same_dialect():
    # The Engine picks a Kokoro Voice's phonemizer from its id prefix, the
    # key `voices-v1.0.bin` ships it under, while the picker reads the
    # `language` field. The two are curated by hand, so this is what stops
    # a British Voice being listed as American or spoken with the wrong G2P.
    from readily_engine.loading.styletts2.kokoro import voice_dialect

    languages = {"american": "en-US", "british": "en-GB"}
    by_id = {entry.id: entry for entry in load_manifest().models}
    for voice in by_id["kokoro:82m"].voices:
        assert voice.language == languages[voice_dialect(voice.id)], voice.id


def test_every_committed_preview_clip_is_actually_bundled():
    # A `preview` naming a clip that is not in `public/previews/` is a 404
    # in the Catalog sheet, and the sheet's whole argument is that a Voice
    # can be auditioned before its Voice Model is downloaded. Curation
    # writes the path and the clip together; this is what catches a
    # hand-edited manifest that writes only one of them.
    previews = MANIFEST_PATH.parents[1] / "public" / "previews"
    for entry in load_manifest().models:
        for voice in entry.voices:
            if voice.preview is None:
                continue
            assert (previews / voice.preview).is_file(), f"{entry.id} {voice.id}"


def test_no_bundled_preview_clip_is_orphaned():
    # The other direction. A clip whose Voice was dropped or renamed is
    # shipped in the app bundle and referenced by nothing; curation sweeps
    # the entry it rewrites, and this is what catches the one it did
    # not — a whole entry removed by hand, or a clip committed by mistake.
    previews = MANIFEST_PATH.parents[1] / "public" / "previews"
    assert previews.is_dir(), "a missing preview root would pass this vacuously"
    named = {
        voice.preview
        for entry in load_manifest().models
        for voice in entry.voices
        if voice.preview is not None
    }
    for clip in previews.rglob("*.m4a"):
        assert str(clip.relative_to(previews)) in named, clip


def test_a_first_block_longer_than_the_chunk_budget_is_refused():
    # ADR 0002 §2: the first Block is *short* so first audio is one quick
    # inference. Longer than the budget it is not a first Block at all.
    with pytest.raises(ValidationError):
        one_model(tunables={**ENTRY["tunables"], "first_block_chars": 451})


@pytest.mark.parametrize(
    "tunables",
    [
        {"chunk_budget_chars": 0},
        {"first_block_chars": 0},
        {"pause_sentence_ms": -1},
        {"pause_paragraph_break_ms": -1},
        {"pause_policy_version": 3},
    ],
    ids=["budget", "first-block", "sentence", "paragraph-break", "policy-version"],
)
def test_a_nonsense_tunable_is_refused(tunables: dict):
    with pytest.raises(ValidationError):
        one_model(tunables={**ENTRY["tunables"], **tunables})


def test_every_committed_entry_carries_the_chunker_and_assembler_tunables():
    for entry in load_manifest().models:
        assert entry.tunables.first_block_chars <= entry.tunables.chunk_budget_chars
        assert entry.tunables.pause_paragraph_break_ms >= (
            entry.tunables.pause_sentence_ms
        )


def test_a_manifest_from_a_future_schema_is_refused():
    # ADR 0003 §1 carries schema_version from day one so a remote+signed
    # catalog is an addition. An Engine that guessed at an unknown shape
    # would be guessing about hashes.
    with pytest.raises(ValidationError):
        manifest(schema_version=2)


def test_a_manifest_with_no_models_is_refused():
    with pytest.raises(ValidationError):
        manifest(models=[])


def test_loading_a_manifest_that_is_not_json_fails_loudly(tmp_path: Path):
    broken = tmp_path / "manifest.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        load_manifest(broken)


def test_loading_an_invalid_manifest_fails_loudly(tmp_path: Path):
    invalid = tmp_path / "manifest.json"
    invalid.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_manifest(invalid)


@pytest.mark.parametrize(
    "hand_edited",
    [
        "../../etc/passwd.m4a",  # climbing out of the preview root
        "/etc/passwd.m4a",  # absolute
        "//evil.example/clip.m4a",  # protocol-relative once it is a URL
        "https://evil.example/clip.m4a",  # not a path at all
        "acme\\1m\\narrator.m4a",  # windows separator
        "acme/1m/narrator.m4a?x=1",  # a query smuggled onto the path
        "acme//narrator.m4a",  # an empty segment
        "acme/./narrator.m4a",
        "",
    ],
    ids=[
        "climbs-out",
        "absolute",
        "protocol-relative",
        "remote",
        "backslash",
        "query",
        "empty-segment",
        "dot-segment",
        "empty",
    ],
)
def test_a_preview_path_that_could_leave_the_app_bundle_is_refused(hand_edited):
    # The shell loads this as audio. Anything that resolves off the bundle
    # is the UI reaching the network, which the privacy invariant forbids.
    with pytest.raises(ValidationError):
        one_model(
            voices=[
                {
                    "id": "narrator",
                    "name": "Narrator",
                    "language": "en-US",
                    "preview": hand_edited,
                }
            ]
        )


def test_a_preview_clip_is_an_m4a():
    with pytest.raises(ValidationError):
        one_model(
            voices=[
                {
                    "id": "narrator",
                    "name": "Narrator",
                    "language": "en-US",
                    "preview": "acme/1m/narrator.wav",
                }
            ]
        )


def voice_with_reference(**reference: object) -> dict[str, object]:
    return {
        "id": "narrator",
        "name": "Narrator",
        "language": "en-US",
        "reference": {
            "clip": "acme/1m/narrator.wav",
            "text": "A sentence the clip says.",
            "attribution": None,
            "sha256": "a" * 64,
            **reference,
        },
    }


@pytest.mark.parametrize(
    "hand_edited",
    ["../../etc/passwd.wav", "/etc/passwd.wav", "acme//narrator.wav", ""],
    ids=["climbs-out", "absolute", "empty-segment", "empty"],
)
def test_a_reference_path_that_could_leave_the_app_bundle_is_refused(hand_edited):
    with pytest.raises(ValidationError):
        one_model(voices=[voice_with_reference(clip=hand_edited)])


def test_a_reference_clip_is_a_wav():
    with pytest.raises(ValidationError):
        one_model(voices=[voice_with_reference(clip="acme/1m/narrator.m4a")])


def test_a_reference_transcript_cannot_be_blank():
    with pytest.raises(ValidationError):
        one_model(voices=[voice_with_reference(text="  ")])


@pytest.mark.parametrize("digest", [None, "", "a" * 63, "A" * 64, "g" * 64])
def test_a_reference_requires_a_lowercase_sha256(digest):
    with pytest.raises(ValidationError):
        one_model(voices=[voice_with_reference(sha256=digest)])


def test_a_voice_may_carry_a_reference_and_most_do_not():
    pinned = one_model(voices=[voice_with_reference()]).models[0].voices[0]
    assert pinned.reference is not None
    assert pinned.reference.clip == "acme/1m/narrator.wav"
    assert pinned.reference.text == "A sentence the clip says."
    assert one_model().models[0].voices[0].reference is None


def clip_attribution(**overrides: object) -> dict[str, object]:
    """A consented corpus clip's credit, as a curator declares it."""
    return {
        "license": "CC-BY-4.0",
        "creator": "CSTR, University of Edinburgh",
        "copyright_notice": "Copyright 2019 University of Edinburgh",
        "source": "https://datashare.ed.ac.uk/handle/10283/3443",
        "modified": True,
        **overrides,
    }


def test_a_reference_clip_from_a_consented_corpus_carries_its_credit():
    pinned = (
        one_model(voices=[voice_with_reference(attribution=clip_attribution())])
        .models[0]
        .voices[0]
    )

    assert pinned.reference is not None
    assert pinned.reference.attribution is not None
    assert pinned.reference.attribution.license == "CC-BY-4.0"
    assert pinned.reference.attribution.creator == "CSTR, University of Edinburgh"
    assert str(pinned.reference.attribution.source) == (
        "https://datashare.ed.ac.uk/handle/10283/3443"
    )
    assert pinned.reference.attribution.modified is True


def test_a_reference_clip_readily_recorded_declares_no_credit():
    # Explicit, like an entry's own attribution: a curator who forgets the
    # slot gets a refusal rather than a corpus clip passing as Readily's.
    assert (
        one_model(voices=[voice_with_reference(attribution=None)])
        .models[0]
        .voices[0]
        .reference.attribution
        is None
    )
    forgotten = voice_with_reference()
    del forgotten["reference"]["attribution"]
    with pytest.raises(ValidationError, match="attribution"):
        one_model(voices=[forgotten])


def test_a_reference_clip_licence_must_be_one_that_asks_for_attribution():
    # The clip slot exists to carry a credit, so a licence that asks for
    # none has nothing to put there; a clip under one is Readily's own.
    with pytest.raises(ValidationError, match="reader-facing attribution"):
        one_model(
            voices=[
                voice_with_reference(attribution=clip_attribution(license="Apache-2.0"))
            ]
        )


def test_a_reference_clip_licence_must_be_on_the_allowlist():
    with pytest.raises(ValidationError, match="allowlist"):
        one_model(
            voices=[
                voice_with_reference(
                    attribution=clip_attribution(license="CC-BY-NC-4.0")
                )
            ]
        )


def test_a_reference_clip_credit_is_not_blank_and_points_at_a_page():
    with pytest.raises(ValidationError):
        one_model(
            voices=[voice_with_reference(attribution=clip_attribution(creator=" "))]
        )
    with pytest.raises(ValidationError):
        one_model(
            voices=[
                voice_with_reference(attribution=clip_attribution(source="datashare"))
            ]
        )


def committed_references():
    """Every (entry, voice, clip path) that carries a Voice Reference. The
    tests over it assert the list is non-empty so a manifest that lost its
    references cannot pass them vacuously."""
    found = [
        (entry, voice, REFERENCE_ROOT / voice.reference.clip)
        for entry in load_manifest().models
        for voice in entry.voices
        if voice.reference is not None
    ]
    assert found, "no committed Voice Reference to check"
    return found


def test_every_committed_reference_clip_is_bundled_in_the_format_the_lane_reads():
    for entry, voice, clip in committed_references():
        assert clip.is_file(), f"{entry.id} {voice.id}"
        assert len(
            voice_references(entry, REFERENCE_ROOT)[
                voice.id, voice.reference.sha256
            ].pcm
        ), clip


def test_no_bundled_reference_clip_is_orphaned():
    assert REFERENCE_ROOT.is_dir(), "a missing reference root passes vacuously"
    named = {
        voice.reference.clip
        for entry in load_manifest().models
        for voice in entry.voices
        if voice.reference is not None
    }
    for clip in REFERENCE_ROOT.rglob("*.wav"):
        assert str(clip.relative_to(REFERENCE_ROOT)) in named, clip


# sha256 of a reference's transcript followed by its clip bytes, keyed by the
# entry version it was qualified under. The Manifest pin catches WAV bytes
# edited under the same path; this table is the record of which transcript
# and bytes each version was qualified with, so a re-pin without a version
# bump is refused. Version 2 is the curated clip; version 3 is the same clip
# with its tail reshaped so the last word finishes before the clip ends.
# Version 4 pins the same bytes in the Manifest and retires older cached
# audio, and version 5 retires audio narrated before Qwen was told the
# Block's language. The 1.7B entry qualified at version 1 with its own copy
# of that clip, and VoxCPM2 at version 1 with another. The same versions
# gained the twelve corpus clips below: adding a Voice swaps nothing a reader
# has cached, so the entries did not bump.
QUALIFIED_REFERENCE_DIGESTS = {
    ("qwen3-tts:0.6b", "Chelsie"): {
        2: "8c6af965f79b192c1770617c614c9e02e67d9f26fbb49e17119a7e21e0a1cd5c",
        3: "f7cb316af5cab244689b6ee46f910610c64ac52a9c560d3379ee4a97b6f35cdc",
        4: "f7cb316af5cab244689b6ee46f910610c64ac52a9c560d3379ee4a97b6f35cdc",
        5: "f7cb316af5cab244689b6ee46f910610c64ac52a9c560d3379ee4a97b6f35cdc",
    },
    ("qwen3-tts:1.7b", "Chelsie"): {
        1: "f7cb316af5cab244689b6ee46f910610c64ac52a9c560d3379ee4a97b6f35cdc",
    },
    ("voxcpm2:2b", "Chelsie"): {
        1: "f7cb316af5cab244689b6ee46f910610c64ac52a9c560d3379ee4a97b6f35cdc",
    },
}

# The twelve clips `engine/tools/reference_clips.py` cuts from VCTK and Hi-Fi
# TTS, shared byte-for-byte by every cloning entry.
CORPUS_REFERENCE_DIGESTS = {
    "Avery": "993bdfea7a6367c9efcf165c76630d36d429318da1bf81bef4c4d82efb67b75d",
    "Mason": "d48358d9ad223344fd6b1c53bd1ce190258b5e0a10b2dac419ec0f9b6556506e",
    "Margaret": "47a9b9f0161b7a3a92a488f8a322cb310e87cbc5d19b56eae3980a3a08c5bfc4",
    "Walter": "3f237cb772f3f68bfdac5f397301e48087b9620b5b4d3577dd7db818fc81d68b",
    "Imogen": "b332487dfefa7973ac53b48a6d159604a330ca5a0eb4b5dc217fbceb40736ef1",
    "Oliver": "bdfe2010f3c33ea417488bb49d2274f5a602d751d8f7d49785857c8311036ddb",
    "Eleanor": "ea93742e7487bef241839b812b093697436129837211dd092707e27af4670453",
    "Jack": "287730118561c60ea5f21dce8e9aa2dcece4eba4ad1ae6529314c7c33f084cc5",
    "Priya": "1c0ff7e724b1f0f9c5d0143ce99a2c6c47a237b6a457bbb47507de703370b578",
    "Arjun": "7bb21ae440b89d108b277dbe5cbfae091390cd878708c6d020169eb2421d8dd0",
    "Thandi": "70eb11715eccb24fd6bf31e85e321a2eef4ceab0f2ba883f0b815b11bc0f7b1a",
    "Sipho": "8913e494099ed60e7995ad7852d53bd899ecf465b914104979e8737c20691242",
}
QUALIFIED_REFERENCE_DIGESTS.update(
    {
        (entry, voice): {version: digest}
        for entry, version in [
            ("qwen3-tts:0.6b", 5),
            ("qwen3-tts:1.7b", 1),
            ("voxcpm2:2b", 1),
        ]
        for voice, digest in CORPUS_REFERENCE_DIGESTS.items()
    }
)


def test_every_committed_reference_is_the_bytes_its_entry_version_was_qualified_with():
    for entry, voice, clip in committed_references():
        digests = QUALIFIED_REFERENCE_DIGESTS.get((entry.id, voice.id))
        assert digests, f"{entry.id} {voice.id}: add its digest to the table"
        assert entry.version in digests, (
            f"{entry.id} version {entry.version} has no qualified reference"
        )
        digest = hashlib.sha256(voice.reference.text.encode() + clip.read_bytes())
        assert digest.hexdigest() == digests[entry.version], (
            f"{clip} changed under version {entry.version}; bump the version"
        )


def test_every_committed_reference_ends_in_whole_tokens_of_near_silence():
    """The lane's talker imitates how its reference ends. This is the
    floor every bundled clip has to clear: a whole number of codec tokens and
    at least 320 ms below -60 dBFS at the end, which the version 2 clip
    (50 ms) does not. It cannot tell a ramped ending from plain padding after
    a hard stop; the digest test above pins the exact bytes that qualified."""
    samples_per_token = int(REFERENCE_SAMPLE_RATE / CODEC_TOKENS_PER_SECOND)
    frame = REFERENCE_SAMPLE_RATE // 100
    floor = 10 ** (-60 / 20)
    for _entry, _voice, clip in committed_references():
        rate, pcm = read_wav(clip)
        assert rate == REFERENCE_SAMPLE_RATE, clip
        assert len(pcm) % samples_per_token == 0, f"{clip}: {len(pcm)} samples"
        _, rms = frame_rms(pcm, frame)
        quiet = 0
        for level in reversed(rms):
            if level > floor:
                break
            quiet += 1
        assert quiet * 10 >= 320, f"{clip} ends with {quiet * 10} ms below -60 dBFS"


def test_a_voice_may_carry_a_preview_clip_and_may_have_none_yet():
    with_clip = one_model(
        voices=[
            {
                "id": "narrator",
                "name": "Narrator",
                "language": "en-US",
                "preview": "acme/1m/narrator.m4a",
            }
        ]
    )
    assert with_clip.models[0].voices[0].preview == "acme/1m/narrator.m4a"
    # Curation generates clips separately from pinning hashes, so an entry
    # whose clips do not exist yet is a Catalog the Engine still serves.
    assert one_model().models[0].voices[0].preview is None


@pytest.mark.parametrize("hand_edited", [0, -1, "half a gig"])
def test_a_ram_class_that_is_not_a_positive_size_is_refused(hand_edited):
    with pytest.raises(ValidationError):
        one_model(ram_class_gb=hand_edited)


def test_an_overflowed_ram_class_is_refused():
    with pytest.raises(ValidationError):
        one_model(ram_class_gb=1e309)


def test_the_committed_manifest_states_a_memory_class_for_every_entry():
    assert all(entry.ram_class_gb > 0 for entry in load_manifest().models)


def test_word_times_are_a_language_capability_independent_of_tier():
    entries = {entry.id: entry for entry in load_manifest().models}
    assert entries["kokoro:82m"].reports_word_times("en-US")
    assert entries["kokoro:82m"].reports_word_times("en-GB")
    assert not entries["kokoro:82m"].reports_word_times("ja-JP")
    assert entries["kitten-tts:15m"].reports_word_times("en-US")
    for name in (
        "qwen3-tts:0.6b",
        "qwen3-tts:1.7b",
        "chatterbox:turbo",
        "supertonic:66m",
        "supertonic:99m",
        "vibevoice-realtime:0.5b",
        "voxcpm2:2b",
    ):
        assert not entries[name].reports_word_times("en-US")


# Kokoro's English presets that pass curation's artifact checks, minus
# `am_adam`, which VOICES.md grades F+. `bm_lewis` passed while he was read
# with the American G2P and crackles once he is read with the British one.
KOKORO_ENGLISH_VOICES = frozenset(
    {
        "af_heart", "af_aoede", "af_bella", "af_kore", "af_nicole", "af_nova",
        "af_river", "af_sarah", "am_echo", "am_eric", "am_onyx",
        "bf_emma", "bm_daniel",
    }
)  # fmt: skip
# Emma's pin was earned on American audio. Restoring it needs a British
# `--check-simple --voice bf_emma` run and a curator listening to it.
KOKORO_SIMPLE_VOICES = frozenset({"af_heart", "af_bella", "af_nicole"})


def test_kokoro_offers_the_english_voices_that_cleared_curation():
    entry = load_manifest().find("kokoro:82m")

    assert {voice.id for voice in entry.voices} == KOKORO_ENGLISH_VOICES
    assert {
        voice.id for voice in entry.voices if qualified(entry, voice.id) is not None
    } == KOKORO_SIMPLE_VOICES
    assert entry.default_voice == "af_heart"
    assert {voice.language for voice in entry.voices} == {"en-US", "en-GB"}


def test_every_committed_qualification_still_matches_its_recipe():
    for entry in load_manifest().models:
        for voice in entry.voices:
            if voice.qualification is not None:
                assert qualified(entry, voice.id) is not None, f"{entry.id} {voice.id}"


# Each entry's default Voice as today's code digests it: the recipe digest a
# qualification pins, and the Segment key of one fixed Block. A change to how
# a Generation Record serialises its parameters moves these, and with them
# every committed qualification and every cached Segment on every install.
COMMITTED_DIGESTS = {
    "kokoro:82m": (
        "75ffb3bccf41ef89e637ce8bdd0606ad55a619e1638adbfb1487b9155801c0a5",
        "cc4971feb2adff5c0b70ee967b97dfeb03f592bc2bca79665d6ad3fb006c1220",
    ),
    "qwen3-tts:0.6b": (
        "cf3abdde92dc035ae35f0972e0916904d3e544dce1a4c8e18de68db7180ab920",
        "ce4ede13de3d6b6893b8c715d046ef91f928c66c2d90d282cb4f960e3647cf80",
    ),
    "qwen3-tts:1.7b": (
        "8a514cd8436ccf5f215329c693f147f36144c1e8115c9c9a8ed379023f8c9d15",
        "8b029bbb8e3cc1d836288f2f69ff8e2389dc69c9e12bd774515829071450259a",
    ),
    "kitten-tts:15m": (
        "52c58496099c42a89a48bcdd1a2bfe364775524d89e3a950d44076301655669d",
        "049e32b5719c43f4753b8f2b89e3f6a871f13a1730264fb418186903236a76ab",
    ),
    "chatterbox:turbo": (
        "181f6b3443f066e0aeff6aa60857496debcabd409ec6cc57c135692690eca46b",
        "04f7e5c3636e32b2bac43032d4639a37ea734d5957b9820946e0b9db16ff804a",
    ),
    "supertonic:66m": (
        "b7ff54d7b0012f67a2d1abd61c007d094938ebafe8161b813a1989d17a65ff26",
        "cb051f78a140ee5a27909534c242a94e44580a7a8894789da75429664eb7bfe1",
    ),
    "supertonic:99m": (
        "cec9eadd5fcc1a75452243599ca7f97340623b4c85d4999762f0ca7a43b4eb6e",
        "73cb08a606257b30564afd0c9cffda6faf5cbe9b2a74f8a13b669ce082b7724e",
    ),
    "vibevoice-realtime:0.5b": (
        "c0a5557f554d0200f9a6ef99f9b2f9d10c55e73e3ed0b50dc119fdfb7c285bdf",
        "c71c5867bc4320397b9f80eb810bfe4c045443c3a6c9dab510d66772a4888b9c",
    ),
    "voxcpm2:2b": (
        "e3d499491ea054d25b89d3e9e0ae2e1e48e90d22f040b7f28b76b28bfacc40ee",
        "9839aa1d31a2d1496fde3a82a67eab9514fc0d8e1e6c22dc080b562d072dc489",
    ),
}


def test_every_committed_recipe_keeps_its_digest():
    manifest = load_manifest()
    assert {entry.id for entry in manifest.models} == COMMITTED_DIGESTS.keys()
    for entry in manifest.models:
        record = GenerationRecord.for_entry(entry, entry.default_voice, "Hello.")
        assert (
            recipe_digest(entry, entry.default_voice),
            record.key,
        ) == COMMITTED_DIGESTS[entry.id], entry.id


def test_a_whole_number_override_of_a_real_knob_keeps_its_segment_key():
    # The webview's JSON writes 1.0 as 1. Records have always carried a
    # real-valued knob as a float, so the Segment cached before still matches.
    entry = load_manifest().find("qwen3-tts:0.6b")
    composed = entry.compose({"temperature": 1, "top_p": 1, "top_k": 40}).entry
    record = GenerationRecord.for_entry(composed, "Chelsie", "Hello.")
    assert '"temperature":1.0,"top_k":40,"top_p":1.0' in record.canonical_json()
    assert record.key == (
        "c5401e42d1da95043c6a54f8b0cf9b5067113040ebf3645436bad71749743fb1"
    )
