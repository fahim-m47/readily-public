"""The download-verify-promote seam and its invariant (ADR 0003 §3):
a promoted model directory exists ⇔ its contents are verified and complete.

Every test drives the real `ModelStore` against a temp data dir with a fake
fetch callable — the store itself never touches the network, so nothing
here does either.
"""

import os
from pathlib import Path

import pytest
from conftest import FILES, make_entry, writing_fetch

from readily_engine.catalog import LICENCE_OBLIGATIONS, CatalogEntry
from readily_engine.catalog.licences import licence_text
from readily_engine.store import ModelStore, StoreUnwritable, VerificationError
from readily_engine.store.derived import output_tail

DERIVED = {"path": "model.timed.onnx", "source": "model.onnx", "outputs": ["duration"]}


@pytest.fixture
def store(tmp_path: Path) -> ModelStore:
    return ModelStore(tmp_path)


def test_the_promoted_layout_is_models_name_tag(store: ModelStore):
    entry = make_entry(FILES)

    assert store.promoted_dir(entry) == store.data_dir / "models" / "acme" / "1m"


def test_staging_lives_outside_the_models_tree(store: ModelStore):
    # `models/` holds promoted directories and nothing else: anything that
    # scans it (listing, disk usage) must never see half-downloaded state.
    entry = make_entry(FILES)

    staging = store.staging_dir(entry.name, entry.tag)

    assert not staging.is_relative_to(store.data_dir / "models")
    # Same data dir, so the final promote is one same-volume atomic rename.
    assert staging.is_relative_to(store.data_dir)


def test_install_downloads_verifies_and_promotes(store: ModelStore):
    entry = make_entry(FILES)

    store.install(entry, writing_fetch(FILES))

    assert store.installed(entry)
    promoted = store.promoted_dir(entry)
    for path, content in FILES.items():
        assert (promoted / path).read_bytes() == content
    assert not store.staging_dir(entry.name, entry.tag).exists()


def stage_files(store: ModelStore, entry: CatalogEntry, files: dict[str, bytes]):
    """Fill staging by hand, the way curation's pinning step does."""
    staging = store.staging_dir(entry.name, entry.tag)
    for path, content in files.items():
        target = staging / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def test_promote_staged_verifies_and_promotes_what_a_curator_staged(
    store: ModelStore,
):
    entry = make_entry(FILES)
    stage_files(store, entry, FILES)

    store.promote_staged(entry)

    assert store.installed(entry)
    promoted = store.promoted_dir(entry)
    for path, content in FILES.items():
        assert (promoted / path).read_bytes() == content


def test_promote_staged_replaces_a_promoted_model_and_sweeps_the_old_one(
    store: ModelStore,
):
    """Unlike `install`, which no-ops once promoted: curation must end with
    the promoted model being exactly the bytes it just staged and pinned,
    whatever an earlier curation left under the same `name:tag`."""
    old = make_entry(FILES)
    store.install(old, writing_fetch(FILES))
    bumped = {path: content + b"-v2" for path, content in FILES.items()}
    entry = make_entry(bumped)
    stage_files(store, entry, bumped)

    store.promote_staged(entry)

    promoted = store.promoted_dir(entry)
    for path, content in bumped.items():
        assert (promoted / path).read_bytes() == content
    assert not list(store.data_dir.rglob("*.deleting"))


def test_promote_staged_refuses_staged_bytes_that_do_not_match_the_pins(
    store: ModelStore,
):
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    stage_files(store, entry, {**FILES, "model.onnx": FILES["model.onnx"][::-1]})

    with pytest.raises(VerificationError, match="does not match its pin"):
        store.promote_staged(entry)

    # The mismatched staging is discarded, the old promoted model is gone —
    # its bytes are not the ones the entry pins either — and no retired
    # tree waits for an app-side delete to sweep it.
    assert not store.installed(entry)
    assert store.staged_bytes(entry) == 0
    assert not list(store.data_dir.rglob("*.deleting"))


def test_install_reports_its_phases_in_order(store: ModelStore):
    entry = make_entry(FILES)
    phases: list[str] = []

    store.install(entry, writing_fetch(FILES), on_phase=phases.append)

    assert phases == ["downloading", "verifying"]


def test_a_corrupted_staged_file_is_rejected_and_staging_discarded(store: ModelStore):
    entry = make_entry(FILES)
    corrupted = {**FILES, "model.onnx": b"tampered bytes"}

    with pytest.raises(VerificationError):
        store.install(entry, writing_fetch(corrupted))

    assert not store.installed(entry)
    assert not store.promoted_dir(entry).exists()
    # A corrupted file cannot heal by resuming — the resumable downloader
    # sees a complete file and skips it — so the whole staging dir goes.
    assert not store.staging_dir(entry.name, entry.tag).exists()


def test_a_missing_staged_file_is_rejected(store: ModelStore):
    entry = make_entry(FILES)
    incomplete = {"model.onnx": FILES["model.onnx"]}

    with pytest.raises(VerificationError):
        store.install(entry, writing_fetch(incomplete))

    assert not store.promoted_dir(entry).exists()
    assert not store.staging_dir(entry.name, entry.tag).exists()


def test_a_staged_symlink_is_rejected_even_when_its_target_hashes_clean(
    store: ModelStore, tmp_path: Path
):
    entry = make_entry(FILES)
    outside = tmp_path / "outside.bin"

    def fetch(entry: CatalogEntry, staging_dir: Path) -> None:
        writing_fetch({"voices/pack.bin": FILES["voices/pack.bin"]})(entry, staging_dir)
        outside.write_bytes(FILES["model.onnx"])
        (staging_dir / "model.onnx").symlink_to(outside)

    with pytest.raises(VerificationError):
        store.install(entry, fetch)

    assert not store.promoted_dir(entry).exists()


def test_a_file_reached_through_a_symlinked_parent_never_promotes(
    store: ModelStore, tmp_path: Path
):
    # The file itself is not a link, so hashing it says "clean"; its parent
    # is. Stripping runs first, which turns that file into a missing one —
    # verification then refuses it, instead of a verified-but-incomplete
    # directory being promoted after the link is stripped away.
    entry = make_entry(FILES)
    outside = tmp_path / "outside"
    outside.mkdir()

    def fetch(entry: CatalogEntry, staging_dir: Path) -> None:
        writing_fetch({"model.onnx": FILES["model.onnx"]})(entry, staging_dir)
        (outside / "pack.bin").write_bytes(FILES["voices/pack.bin"])
        (staging_dir / "voices").symlink_to(outside, target_is_directory=True)

    with pytest.raises(VerificationError):
        store.install(entry, fetch)

    assert not store.promoted_dir(entry).exists()
    assert not store.staging_dir(entry.name, entry.tag).exists()
    # Stripping unlinked the link, never the directory it pointed at.
    assert (outside / "pack.bin").read_bytes() == FILES["voices/pack.bin"]


def test_downloader_metadata_never_reaches_the_promoted_directory(store: ModelStore):
    # huggingface_hub keeps resume metadata under `.cache/` inside the
    # download dir. The promoted directory holds exactly the manifest's
    # files — verified bytes and nothing else.
    entry = make_entry(FILES)
    with_extras = {
        **FILES,
        ".cache/huggingface/download/model.onnx.metadata": b"etag gunk",
    }

    store.install(entry, writing_fetch(with_extras))

    promoted = store.promoted_dir(entry)
    found = {
        path.relative_to(promoted).as_posix()
        for path in promoted.rglob("*")
        if path.is_file()
    }
    assert found == {*FILES, "LICENSE"}


def test_the_licence_travels_with_the_weights(store: ModelStore):
    entry = make_entry(FILES, license="Apache-2.0")

    store.install(entry, writing_fetch(FILES))

    written = store.promoted_dir(entry) / "LICENSE"
    assert written.read_text(encoding="utf-8") == licence_text("Apache-2.0")
    assert not (store.promoted_dir(entry) / "Notice").exists()


def test_a_licence_that_prescribes_a_notice_gets_one_written(store: ModelStore):
    entry = make_entry(FILES, license="llama3.1")

    store.install(entry, writing_fetch(FILES))

    notice = store.promoted_dir(entry) / "Notice"
    assert notice.read_text(encoding="utf-8") == LICENCE_OBLIGATIONS["llama3.1"].notice


def test_upstream_s_pinned_licence_file_is_the_one_that_ships(store: ModelStore):
    with_licence = {**FILES, "LICENSE": b"Copyright (c) 2024 Acme\n\nMIT..."}
    entry = make_entry(with_licence, license="MIT", copyright_notice="Acme")

    store.install(entry, writing_fetch(with_licence))

    written = store.promoted_dir(entry) / "LICENSE"
    assert written.read_bytes() == with_licence["LICENSE"]


def test_upstream_s_pinned_notice_file_is_the_one_that_ships(store: ModelStore):
    prescribed = LICENCE_OBLIGATIONS["llama3.1"].notice
    with_notice = {**FILES, "NOTICE": f"{prescribed}\n".encode()}
    entry = make_entry(with_notice, license="llama3.1")

    store.install(entry, writing_fetch(with_notice))

    promoted = store.promoted_dir(entry)
    assert (promoted / "NOTICE").read_bytes() == with_notice["NOTICE"]
    assert {path.name for path in promoted.iterdir()} == {
        *{Path(name).parts[0] for name in FILES},
        "LICENSE",
        "NOTICE",
    }


def test_a_fetch_failure_keeps_staging_so_a_retry_resumes(store: ModelStore):
    entry = make_entry(FILES)
    partial = {"model.onnx": FILES["model.onnx"]}

    def interrupted(entry: CatalogEntry, staging_dir: Path) -> None:
        writing_fetch(partial)(entry, staging_dir)
        raise OSError("network dropped mid-download")

    with pytest.raises(OSError):
        store.install(entry, interrupted)

    assert not store.installed(entry)
    staged = store.staging_dir(entry.name, entry.tag) / "model.onnx"
    assert staged.read_bytes() == FILES["model.onnx"]

    store.install(entry, writing_fetch({"voices/pack.bin": FILES["voices/pack.bin"]}))
    assert store.installed(entry)


def test_install_is_a_no_op_once_promoted(store: ModelStore):
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))

    def never(entry: CatalogEntry, staging_dir: Path) -> None:
        raise AssertionError("a promoted model must never be re-fetched")

    store.install(entry, never)
    assert store.installed(entry)


def test_repair_writes_the_stores_files_beside_every_installed_model(
    store: ModelStore,
):
    # `install` backfills, but an installed Catalog row never installs
    # again — it only offers Delete — so the Engine repairs every promoted
    # directory once at startup, and touches nothing that is not promoted.
    entry = make_entry(FILES, license="llama3.1")
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "LICENSE").unlink()
    (promoted / "Notice").unlink()
    absent = make_entry(FILES, name="absent")

    store.repair([entry, absent])

    assert (promoted / "LICENSE").read_text() == licence_text("llama3.1")
    assert (promoted / "Notice").read_text() == LICENCE_OBLIGATIONS["llama3.1"].notice
    assert not store.promoted_dir(absent).exists()


def test_repair_logs_a_directory_it_cannot_write_and_carries_on(
    store: ModelStore, caplog: pytest.LogCaptureFixture
):
    entry = make_entry(FILES, license="llama3.1")
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "LICENSE").unlink()
    later = make_entry(FILES, name="later", license="llama3.1")
    store.install(later, writing_fetch(FILES))
    (store.promoted_dir(later) / "LICENSE").unlink()
    promoted.chmod(0o500)
    try:
        with caplog.at_level("WARNING"):
            store.repair([entry, later])
    finally:
        promoted.chmod(0o700)

    assert not (promoted / "LICENSE").exists()
    assert store.installed(entry)
    assert (store.promoted_dir(later) / "LICENSE").exists()
    assert any("acme:1m" in record.message for record in caplog.records)
    assert all(record.levelname == "WARNING" for record in caplog.records)


def test_a_licence_the_table_has_since_corrected_is_rewritten_at_startup(
    store: ModelStore,
):
    # The files the store writes come from its own table, so when the table
    # changes what an entry's licence says, a model already promoted under
    # it gets the corrected text at the next startup, and any half-written
    # copy a kill left beside the weights is swept.
    entry = make_entry(FILES, license="llama3.1")
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "LICENSE").write_text("the terms an earlier table carried")
    stale = promoted / f"LICENSE.{'0' * 32}.part"
    stale.write_text("half of them")

    store.repair([entry])

    assert (promoted / "LICENSE").read_text() == licence_text("llama3.1")
    assert not stale.exists()


def test_the_sweep_takes_only_the_store_s_own_litter(store: ModelStore):
    # A manifest may pin a file that happens to be named like the litter a
    # killed write leaves; only the shape the store itself writes is swept.
    files = {
        **FILES,
        "LICENSE.weights.part": b"pinned",
        f"LICENSE.{'f' * 32}.part": b"too",
    }
    entry = make_entry(files, license="llama3.1")
    store.install(entry, writing_fetch(files))
    promoted = store.promoted_dir(entry)

    assert (promoted / "LICENSE.weights.part").read_bytes() == b"pinned"
    assert (promoted / f"LICENSE.{'f' * 32}.part").read_bytes() == b"too"


def test_a_licence_that_is_not_text_is_rewritten_rather_than_choked_on(
    store: ModelStore,
):
    entry = make_entry(FILES, license="llama3.1")
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "LICENSE").write_bytes(b"\xff\xfe not utf-8")

    store.repair([entry])

    assert (promoted / "LICENSE").read_text() == licence_text("llama3.1")


def test_a_write_that_fails_leaves_no_partial_file_beside_the_weights(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
):
    entry = make_entry(FILES, license="llama3.1")
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "LICENSE").unlink()

    def full_disk(source: object, target: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "rename", full_disk)
    store.repair([entry])

    assert not list(promoted.glob("*.part"))


def test_a_derived_graph_is_written_beside_its_pinned_source(store: ModelStore):
    entry = make_entry(FILES, derived_files=[DERIVED])

    store.install(entry, writing_fetch(FILES))

    promoted = store.promoted_dir(entry)
    assert (promoted / "model.onnx").read_bytes() == FILES["model.onnx"]
    assert (promoted / "model.timed.onnx").read_bytes() == FILES[
        "model.onnx"
    ] + output_tail(["duration"])


def test_repair_rebuilds_a_derived_graph_that_is_missing_or_altered(
    store: ModelStore,
):
    entry = make_entry(FILES, derived_files=[DERIVED])
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    expected = (promoted / "model.timed.onnx").read_bytes()
    (promoted / "model.timed.onnx").unlink()
    store.repair([entry])
    assert (promoted / "model.timed.onnx").read_bytes() == expected

    (promoted / "model.timed.onnx").write_bytes(b"not the derivation")
    store.repair([entry])
    assert (promoted / "model.timed.onnx").read_bytes() == expected


@pytest.mark.parametrize("missing", ["model.timed.onnx", "model.onnx"])
def test_a_read_only_broken_directory_is_retired_and_reinstalled(
    store: ModelStore, caplog: pytest.LogCaptureFixture, missing: str
):
    # A directory the store cannot write into is the one it most needs to
    # retire; the same-parent rename needs no write access to it.
    entry = make_entry(FILES, derived_files=[DERIVED])
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / missing).unlink()
    promoted.chmod(0o500)
    try:
        store.repair([entry])
        assert not store.installed(entry)
        assert not promoted.exists()
        assert "acme:1m" in caplog.text

        store.install(entry, writing_fetch(FILES))
    finally:
        for retired in promoted.parent.glob("*.deleting"):
            retired.chmod(0o700)

    assert store.installed(entry)
    assert (promoted / "model.timed.onnx").read_bytes() == FILES[
        "model.onnx"
    ] + output_tail(["duration"])


@pytest.mark.parametrize("old_graph", [None, b"wrong graph"])
def test_failed_derived_write_retires_only_the_broken_model(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch, old_graph: bytes | None
):
    entry = make_entry(FILES, derived_files=[DERIVED])
    later = make_entry(FILES, name="later", derived_files=[DERIVED])
    store.install(entry, writing_fetch(FILES))
    store.install(later, writing_fetch(FILES))
    graph = store.promoted_dir(entry) / "model.timed.onnx"
    graph.unlink()
    if old_graph is not None:
        graph.write_bytes(old_graph)
    (store.promoted_dir(later) / "model.timed.onnx").unlink()
    rename = os.rename

    def disk_full_for_graph(source, target):
        if target == graph:
            raise OSError("no space left on device")
        rename(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", disk_full_for_graph)
        store.repair([entry, later])

    assert not store.installed(entry)
    assert not store.promoted_dir(entry).exists()
    assert store.installed(later)
    store.install(entry, writing_fetch(FILES))
    assert store.installed(entry)


def test_failed_licence_write_does_not_prevent_derived_repair(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
):
    entry = make_entry(FILES, derived_files=[DERIVED])
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "LICENSE").unlink()
    (promoted / "model.timed.onnx").unlink()
    rename = os.rename

    def deny_licence(source, target):
        if target == promoted / "LICENSE":
            raise PermissionError("licence cannot be written")
        rename(source, target)

    monkeypatch.setattr(os, "rename", deny_licence)
    store.repair([entry])

    assert store.installed(entry)
    assert (promoted / "model.timed.onnx").read_bytes() == FILES[
        "model.onnx"
    ] + output_tail(["duration"])
    assert not (promoted / "LICENSE").exists()


def test_unremovable_derived_partial_does_not_withhold_a_valid_model(store: ModelStore):
    entry = make_entry(FILES, derived_files=[DERIVED])
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    stale = promoted / f"model.timed.onnx.{'0' * 32}.part"
    stale.write_bytes(b"unfinished graph")
    promoted.chmod(0o500)
    try:
        store.repair([entry])

        assert store.installed(entry)
        assert (promoted / "model.timed.onnx").read_bytes() == FILES[
            "model.onnx"
        ] + output_tail(["duration"])
    finally:
        if promoted.exists():
            promoted.chmod(0o700)


def test_a_derivation_declared_since_promotion_is_written_at_startup(
    store: ModelStore,
):
    store.install(make_entry(FILES), writing_fetch(FILES))
    entry = make_entry(FILES, derived_files=[DERIVED])

    store.repair([entry])

    assert store.installed(entry)
    assert (store.promoted_dir(entry) / "model.timed.onnx").read_bytes() == FILES[
        "model.onnx"
    ] + output_tail(["duration"])


def test_staged_bytes_counts_the_partial_download(store: ModelStore):
    entry = make_entry(FILES)
    partial = {"model.onnx": FILES["model.onnx"]}

    def interrupted(entry: CatalogEntry, staging_dir: Path) -> None:
        writing_fetch(partial)(entry, staging_dir)
        raise OSError("network dropped")

    assert store.staged_bytes(entry) == 0
    with pytest.raises(OSError):
        store.install(entry, interrupted)

    assert store.staged_bytes(entry) == len(FILES["model.onnx"])


def test_delete_removes_the_promoted_model(store: ModelStore):
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))

    assert store.delete(entry) is True
    assert not store.installed(entry)
    assert not store.promoted_dir(entry).exists()
    assert store.delete(entry) is False


def test_a_retired_model_is_gone_before_the_slow_purge_runs(store: ModelStore):
    # DownloadManager holds its lock only across `retire`; a kill (or a
    # crash) between the halves must leave nothing `installed` reports and
    # nothing the next purge cannot sweep.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))

    assert store.retire(entry) is True
    assert not store.installed(entry)
    leftovers = list(store.promoted_dir(entry).parent.glob("*.deleting"))
    assert len(leftovers) == 1

    store.purge(entry)
    assert not leftovers[0].exists()
    assert store.retire(entry) is False


def test_purging_the_retired_tree_alone_keeps_an_in_flight_download(
    store: ModelStore,
):
    # `promote_staged` retires the promoted model before promoting the bytes
    # just staged, so it needs the sweep without the staging half: a failure
    # between the two must cost the retired tree, never the download.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    staging = store.staging_dir(entry.name, entry.tag)
    writing_fetch(FILES)(entry, staging)

    assert store.retire(entry) is True
    store.purge_retired(entry)

    assert not list(store.data_dir.rglob("*.deleting"))
    assert store.staged_bytes(entry) > 0


def test_purge_sweeps_a_tree_an_earlier_build_retired_into_staging(
    store: ModelStore,
):
    # Before the same-parent rename, `retire` moved the promoted model to
    # `staging/<name>/<tag>-<uuid>.deleting`. A kill between that retire
    # and its purge, followed by an upgrade, must not strand the tree.
    entry = make_entry(FILES)
    staging = store.staging_dir(entry.name, entry.tag)
    legacy = staging.parent / f"{staging.name}-0123abcd.deleting"
    legacy.mkdir(parents=True)
    (legacy / "model.onnx").write_bytes(FILES["model.onnx"])

    store.purge_retired(entry)

    assert not legacy.exists()
    assert not list(store.data_dir.rglob("*.deleting"))


def test_repair_says_loudly_when_a_broken_model_cannot_be_retired(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    # The second failure mode: the same-parent rename refuses, as
    # on a read-only volume. The broken directory stays promoted, but a
    # marker beside the weights keeps `installed` from calling it so, and
    # repair carries on to the next entry.
    entry = make_entry(FILES)
    later = make_entry(FILES, name="later")
    store.install(entry, writing_fetch(FILES))
    store.install(later, writing_fetch(FILES))
    (store.promoted_dir(entry) / "voices" / "pack.bin").unlink()
    (store.promoted_dir(later) / "voices" / "pack.bin").unlink()
    rename = os.rename

    def refuse_first(src: str | Path, dst: str | Path) -> None:
        if Path(src) == store.promoted_dir(entry):
            raise PermissionError("read-only parent")
        rename(src, dst)

    monkeypatch.setattr(os, "rename", refuse_first)
    store.repair([entry, later])

    assert not store.installed(entry)
    assert store.promoted_dir(entry).is_dir()
    assert "read-only parent" in (store.promoted_dir(entry) / ".broken").read_text()
    assert not store.installed(later)
    assert not store.promoted_dir(later).exists()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "acme:1m" in errors[0].getMessage()
    assert str(store.promoted_dir(entry).parent) in errors[0].getMessage()


def test_a_broken_model_under_an_unwritable_parent_is_not_installed(
    store: ModelStore, caplog: pytest.LogCaptureFixture
):
    # The first failure mode: `models/<name>` itself cannot be
    # written, so the rename that retires the directory has nowhere to
    # go. The promoted directory is still writable, so the store marks it
    # there instead of reporting it installed.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "voices" / "pack.bin").unlink()
    promoted.parent.chmod(0o500)
    try:
        store.repair([entry])

        assert not store.installed(entry)
        assert promoted.is_dir()
        assert store.disk_usage(entry) == 0
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert str(promoted.parent) in errors[0].getMessage()

        with pytest.raises(StoreUnwritable) as refused:
            store.install(entry, writing_fetch(FILES))
        assert str(promoted.parent) in str(refused.value)
        assert not store.installed(entry)
    finally:
        promoted.parent.chmod(0o755)


def test_a_marked_model_is_retired_once_its_parent_is_writable_again(
    store: ModelStore,
):
    # The marker is a record, not a verdict: the next start tries the
    # rename again, and once it works the entry is installable as usual.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "voices" / "pack.bin").unlink()
    promoted.parent.chmod(0o500)
    try:
        store.repair([entry])
        assert not store.installed(entry)
    finally:
        promoted.parent.chmod(0o755)

    store.repair([entry])

    assert not promoted.exists()
    assert not list(store.data_dir.rglob("*.deleting"))
    store.install(entry, writing_fetch(FILES))
    assert store.installed(entry)


def test_a_marked_model_is_replaced_by_the_next_download_without_a_restart(
    store: ModelStore,
):
    # The message tells the reader to give Readily write access to the
    # folder; what they do next is press Download again, not restart, so
    # the download itself retires the marked directory.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "voices" / "pack.bin").unlink()
    promoted.parent.chmod(0o500)
    try:
        store.repair([entry])
        assert not store.installed(entry)
    finally:
        promoted.parent.chmod(0o755)

    store.install(entry, writing_fetch(FILES))

    assert store.installed(entry)
    assert (promoted / "voices" / "pack.bin").exists()
    assert not (promoted / ".broken").exists()
    assert not list(store.data_dir.rglob("*.deleting"))


def test_a_marker_is_removed_when_the_directory_verifies_again(
    store: ModelStore,
):
    # The pinned file came back (a reader restored it by hand) before the
    # parent became writable: nothing needs retiring, and the marker must
    # not keep a verified model uninstalled.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    pinned = promoted / "voices" / "pack.bin"
    original = pinned.read_bytes()
    pinned.unlink()
    promoted.parent.chmod(0o500)
    try:
        store.repair([entry])
        assert not store.installed(entry)
        pinned.write_bytes(original)
        store.repair([entry])
    finally:
        promoted.parent.chmod(0o755)

    assert store.installed(entry)
    assert not (promoted / ".broken").exists()


def test_repair_says_loudly_when_neither_directory_can_be_written(
    store: ModelStore, caplog: pytest.LogCaptureFixture
):
    # Both the parent and the promoted directory refuse: no rename, no
    # marker. The one hole left: the model still looks installed, and the
    # log says so rather than hiding it.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    promoted = store.promoted_dir(entry)
    (promoted / "voices" / "pack.bin").unlink()
    promoted.chmod(0o500)
    promoted.parent.chmod(0o500)
    try:
        store.repair([entry])
    finally:
        promoted.parent.chmod(0o755)
        promoted.chmod(0o755)

    assert store.installed(entry)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "still looks installed" in errors[0].getMessage()


def test_delete_also_discards_any_stale_staging(store: ModelStore):
    entry = make_entry(FILES)

    def interrupted(entry: CatalogEntry, staging_dir: Path) -> None:
        writing_fetch({"model.onnx": FILES["model.onnx"]})(entry, staging_dir)
        raise OSError("network dropped")

    with pytest.raises(OSError):
        store.install(entry, interrupted)

    assert store.delete(entry) is False
    assert not store.staging_dir(entry.name, entry.tag).exists()


def test_disk_usage_reports_promoted_bytes_only(store: ModelStore):
    entry = make_entry(FILES)

    assert store.disk_usage(entry) == 0
    store.install(entry, writing_fetch(FILES))

    written = len(licence_text(entry.license).encode())
    assert store.disk_usage(entry) == sum(len(c) for c in FILES.values()) + written


def test_repair_retires_a_promoted_model_missing_a_pinned_file(store: ModelStore):
    # A promoted directory is verified and complete (ADR 0003 §3); one
    # that lost a pinned file — a user or another tool removed it — is not
    # installed, whatever its name says. Startup retires it, and the next
    # download stages the entry again, verifies every pinned file, and
    # promotes: full verified replacement.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    (store.promoted_dir(entry) / "voices" / "pack.bin").unlink()

    store.repair([entry])

    assert not store.installed(entry)
    assert not store.promoted_dir(entry).exists()
    assert not list(store.data_dir.rglob("*.deleting"))

    store.install(entry, writing_fetch(FILES))

    assert store.installed(entry)
    for path, content in FILES.items():
        assert (store.promoted_dir(entry) / path).read_bytes() == content


def test_repair_retires_a_promoted_model_whose_pinned_file_fails_its_hash(
    store: ModelStore,
):
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    (store.promoted_dir(entry) / "model.onnx").write_bytes(FILES["model.onnx"][::-1])

    store.repair([entry])

    assert not store.installed(entry)
    assert not store.promoted_dir(entry).exists()


def test_a_file_pinned_since_promotion_makes_the_model_need_downloading_again(
    store: ModelStore,
):
    # A later Manifest pins a file under an already-promoted `name:tag` — a
    # licence pinned for the first time on a re-curation. The bundled text
    # is not a stand-in: it fails the hash the entry pins, and for MIT and
    # BSD the holder's notice *is* the licence.
    store.install(make_entry(FILES), writing_fetch(FILES))
    with_licence = {**FILES, "LICENSE": b"Copyright (c) 2024 Acme\n\nMIT..."}
    entry = make_entry(with_licence, license="MIT", copyright_notice="Acme")

    store.repair([entry])

    assert not store.installed(entry)
    store.install(entry, writing_fetch(with_licence))
    assert store.installed(entry)
    written = store.promoted_dir(entry) / "LICENSE"
    assert written.read_bytes() == with_licence["LICENSE"]


def test_repair_names_the_file_it_retired_a_model_for(
    store: ModelStore, caplog: pytest.LogCaptureFixture
):
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    (store.promoted_dir(entry) / "voices" / "pack.bin").unlink()

    with caplog.at_level("WARNING"):
        store.repair([entry])

    assert any(
        "acme:1m" in record.message and "voices/pack.bin" in record.message
        for record in caplog.records
    )


def test_repair_keeps_a_download_the_last_shutdown_interrupted(store: ModelStore):
    # Retiring costs the incomplete promoted tree, never the staging beside
    # it: bytes the Reader already fetched are what the next attempt resumes.
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    (store.promoted_dir(entry) / "voices" / "pack.bin").unlink()
    writing_fetch({"model.onnx": FILES["model.onnx"]})(
        entry, store.staging_dir(entry.name, entry.tag)
    )

    store.repair([entry])

    assert not store.installed(entry)
    assert store.staged_bytes(entry) == len(FILES["model.onnx"])


def test_repair_leaves_a_complete_promoted_model_promoted(store: ModelStore):
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))

    store.repair([entry])

    assert store.installed(entry)
    assert not store.staging_dir(entry.name, entry.tag).exists()


def test_repair_retires_a_promoted_model_whose_pinned_file_cannot_be_read(
    store: ModelStore,
):
    entry = make_entry(FILES)
    store.install(entry, writing_fetch(FILES))
    unreadable = store.promoted_dir(entry) / "model.onnx"
    unreadable.chmod(0o000)

    try:
        store.repair([entry])
    finally:
        if unreadable.exists():
            unreadable.chmod(0o644)

    assert not store.installed(entry)
    assert not store.promoted_dir(entry).exists()
