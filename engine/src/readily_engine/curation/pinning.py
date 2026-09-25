"""Pin a draft's files: download them, then hash what actually arrived.

The whole point of the script: a hash typed by hand is a hash that can
be wrong, and a wrong hash either bricks an install or — pinned against the
wrong bytes — pins the wrong bytes. Nothing here trusts the upstream's own
digests; the SHA-256 that lands in the manifest is computed from the file on
this disk, with `store.file_sha256`, which is the same function the Engine
will later verify the download with.
"""

import os
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from readily_engine.catalog import LICENCE_OBLIGATIONS, ManifestFile, Source
from readily_engine.catalog.licence_table import LICENCE_FILE_NAMES, is_notice_file
from readily_engine.catalog.licences import (
    identify_licence,
    is_licence_file,
    read_licence,
    says_the_prescribed_notice,
)
from readily_engine.curation.draft import CurationError, Draft, SupportDraft
from readily_engine.store import file_sha256

# Curation's download seam: the `download.fetch_files` half that names paths
# rather than pinned files, injected so every test here runs offline.
FetchFiles = Callable[[Source, Sequence[str], Path], None]


def pin(
    draft: Draft | SupportDraft, staging_dir: Path, fetch: FetchFiles
) -> tuple[ManifestFile, ...]:
    """Download the draft's named files into staging and pin each one, plus
    whichever of upstream's own licence and notice files the repo ships.

    The licence file is asked for rather than declared (ADR 0009 §3): a
    curator never has to know which name an upstream chose, and once it is
    pinned it is hash-verified, promoted and deleted with the weights. It is
    also read: a draft whose declared licence is not what upstream's LICENSE
    says is refused.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        licence_files = LICENCE_FILE_NAMES
        # Staging is resumable and a fetch only adds files, so a licence file
        # an earlier revision shipped would still be here after the draft
        # moved to one that dropped it, and would be pinned against a
        # revision that cannot supply it.
        for name in licence_files:
            stale = staging_dir / name
            if stale.is_file() or stale.is_symlink():
                stale.unlink()
        fetch(draft.source, [*draft.files, *licence_files], staging_dir)
        # By the name on disk, not by lookup: on a case-insensitive
        # filesystem every spelling finds the one file upstream shipped, and
        # only the spelling upstream used can be pinned and fetched again.
        present = set(os.listdir(staging_dir))
        shipped = [
            name
            for name in licence_files
            if name in present and (staging_dir / name).is_file()
        ]
        pinned = _pinned(draft, [*draft.files, *shipped], staging_dir)
        _refuse_a_mislabelled_licence(draft, pinned, staging_dir)
        _refuse_a_notice_that_is_not_the_prescribed_one(draft, pinned, staging_dir)
        return pinned
    except Exception:
        # Staging is deliberately resumable, so bytes that arrived stay put
        # — but a draft refused before anything downloaded (a file name the
        # repo does not have, a revision that does not exist) would leave
        # nothing but empty directories in the user's data dir.
        _prune_empty(staging_dir)
        raise


def _pinned(
    draft: Draft | SupportDraft, paths: Sequence[str], staging_dir: Path
) -> tuple[ManifestFile, ...]:
    pinned: list[ManifestFile] = []
    for path in paths:
        staged = staging_dir / path
        # lstat before reading: a symlink's target hashes clean while the
        # link points wherever it likes, and a pin taken through one
        # describes bytes that were never downloaded. Every parent too, not
        # just the leaf — `store._verify` can check the leaf alone because
        # `_keep_only_manifest_files` has already unlinked every path the
        # manifest does not name, and pinning has no such step before it.
        if any(part.is_symlink() for part in _up_to(staged, staging_dir)):
            raise CurationError(f"{path} is reached through a symlink in staging")
        if not staged.is_file():
            raise CurationError(
                f"{path} is not in {draft.source.hf_repo} at "
                f"{draft.source.revision[:12]}"
            )
        size = staged.stat().st_size
        if not size:
            raise CurationError(f"{path} downloaded empty")
        pinned.append(
            ManifestFile(path=path, sha256=file_sha256(staged), size_bytes=size)
        )
    return tuple(pinned)


def _refuse_a_mislabelled_licence(
    draft: Draft | SupportDraft, pinned: Sequence[ManifestFile], staging_dir: Path
) -> None:
    """The weights' own licence file is what the entry records, never the
    repository's tag (ADR 0009 §2), and it says exactly the notice the draft
    declares where the licence leaves room for one (§3)."""
    for file in pinned:
        if not is_licence_file(file.path):
            continue
        text = (staging_dir / file.path).read_text(encoding="utf-8", errors="replace")
        if identify_licence(text, draft.copyright_notice) == draft.license:
            continue
        found, written = read_licence(text)
        if found != draft.license:
            raise CurationError(
                f"{file.path} in {draft.source.hf_repo} is "
                f"{found or 'no licence Readily may hand a reader'}, "
                f"not {draft.license} as the draft declares"
            )
        # Shown in full so the curator can copy it into the draft, once
        # satisfied it is the holder's notice and not a term.
        raise CurationError(
            f"{file.path} in {draft.source.hf_repo} says, where {found} leaves "
            f"room for the holder's notice,\n{written or '(nothing)'}\n"
            f"and the draft's copyright_notice declares\n"
            f"{draft.copyright_notice or '(nothing)'}"
        )


def _up_to(staged: Path, staging_dir: Path) -> list[Path]:
    """`staged` and every directory between it and staging, exclusive."""
    return [staged, *staged.parents[: len(staged.parents) - len(staging_dir.parts)]]


def _prune_empty(staging_dir: Path) -> None:
    """Drop `<name>/<tag>/` and the `<name>` husk above it, but only while
    both are empty — `rmdir` refuses a directory holding a partial download,
    which is exactly the one worth keeping."""
    with suppress(OSError):
        staging_dir.rmdir()
        staging_dir.parent.rmdir()


def _refuse_a_notice_that_is_not_the_prescribed_one(
    draft: Draft | SupportDraft, pinned: Sequence[ManifestFile], staging_dir: Path
) -> None:
    """Upstream's `Notice` ships in place of the one the store would write
    (ADR 0009 §3), so for a licence that prescribes one it has to say exactly
    that. Under any other licence, or any other name, a notice is upstream's
    to fill and is pinned as found."""
    prescribed = LICENCE_OBLIGATIONS[draft.license].notice
    if prescribed is None:
        return
    for file in pinned:
        if not is_notice_file(file.path):
            continue
        text = (staging_dir / file.path).read_text(encoding="utf-8", errors="replace")
        if says_the_prescribed_notice(text, draft.license):
            continue
        raise CurationError(
            f"{file.path} in {draft.source.hf_repo} would ship as the Notice "
            f"{draft.license} prescribes but says\n{text.strip() or '(nothing)'}\n"
            f"where the licence prescribes\n{prescribed}"
        )
