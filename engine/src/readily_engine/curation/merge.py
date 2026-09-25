"""Publish one curated entry: its Voice Preview clips and its manifest row.

Its own module because of what the files are: the manifest is the trust root
every download is checked against (ADR 0003 §1), the clips are bundled into
the app beside it, and the rules for writing them — validate before the
first byte lands, replace whole rows, never leave a torn file behind — are
not argument parsing. `curate` itself writes neither; this is the one step
that does, so a curation run without it mutates nothing a release reads.
"""

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from readily_engine.catalog import CatalogEntry, Manifest, PinnedArtifact, SupportModel
from readily_engine.catalog.recipes import qualified, recipe_digest
from readily_engine.curation.draft import CurationError
from readily_engine.curation.entry import CuratedEntry


def publish(
    manifest_path: Path, preview_root: Path, curated: CuratedEntry
) -> tuple[Path, ...]:
    """Land one curation's staged clips and manifest row together.

    Ordered so an interruption never leaves the manifest naming a clip that
    is not there: the prospective manifest is validated before the first
    clip is copied, the clips land before the manifest row that references
    them, and the orphan sweep runs last — a failure part-way leaves extra
    clips for `git status` to show, never a dangling reference.
    """
    expected = {voice.preview for voice in curated.entry.voices}
    if set(curated.clips) != expected:
        # `curate` stages one clip per Voice; a `CuratedEntry` built any
        # other way could commit a row whose `preview` no copy below ever
        # writes — the dangling reference this whole ordering exists to
        # rule out.
        raise CurationError(
            f"{curated.entry.id}: staged clips do not match the entry's Voices"
        )
    text = _merged_manifest(manifest_path, curated.entry)
    published: list[Path] = []
    for relpath, staged in curated.clips.items():
        clip = preview_root / relpath
        clip.parent.mkdir(parents=True, exist_ok=True)
        _replace_clip(staged, clip)
        published.append(clip)
    _replace_manifest(manifest_path, text)
    _sweep_orphaned_clips(published)
    return tuple(published)


def _merged_manifest(manifest_path: Path, entry: CatalogEntry | SupportModel) -> str:
    """The manifest text with `entry`'s row replaced or appended, validated.

    The whole document is re-serialized from the parsed entry rather than
    patched field by field, so what lands on disk is exactly what the
    Engine's own schema produced — which is what makes a byte-identical
    rewrite mean "nothing changed".
    """
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = entry.model_dump(mode="json")
    models = (
        document.setdefault("support_models", [])
        if isinstance(entry, SupportModel)
        else document["models"]
    )
    for index, model in enumerate(models):
        if (model.get("name"), model.get("tag")) == (entry.name, entry.tag):
            old = type(entry).model_validate(model)
            _require_version_bump(old, entry)
            if isinstance(entry, CatalogEntry) and isinstance(old, CatalogEntry):
                for voice, row in zip(entry.voices, payload["voices"], strict=True):
                    previous = qualified(old, voice.id)
                    if (
                        voice.qualification is None
                        and previous is not None
                        and previous.recipe_sha256 == recipe_digest(entry, voice.id)
                    ):
                        row["qualification"] = previous.model_dump(mode="json")
            models[index] = {
                key: payload[key] for key in model if key in payload
            } | payload
            break
    else:
        models.append(payload)
    Manifest.model_validate(document)
    # `ensure_ascii=False` so a curated entry's prose survives as itself: an
    # escape would rewrite rows nobody curated, and a manifest whose
    # untouched entries change is a regeneration proof that proves nothing.
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _replace_clip(staged: Path, clip: Path) -> None:
    # Not `shutil.copyfile`, which truncates the target before it streams:
    # on a re-publish the target is a clip the committed manifest already
    # names, and an interruption mid-copy would ship a torn audition under
    # a manifest that vouches for it. Same scratch-and-rename discipline as
    # `_replace_manifest`, for the same reason. The `.m4a` stays last so a
    # kill's leftover scratch is a clip no Voice names — the next publish's
    # orphan sweep removes it instead of it hiding from the `*.m4a` glob.
    scratch = clip.with_name(f"{clip.stem}.{uuid4().hex}{clip.suffix}")
    try:
        with staged.open("rb") as source, scratch.open("wb") as handle:
            shutil.copyfileobj(source, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, clip)
    finally:
        scratch.unlink(missing_ok=True)


def _replace_manifest(manifest_path: Path, text: str) -> None:
    # Written beside the target and renamed, so an interrupted write leaves
    # the old manifest rather than half of a new one. Flushed to disk before
    # the rename, so that holds through power loss and not only through a
    # kill: `os.replace` is atomic either way, but a rename can reach the
    # platter ahead of the bytes it points at.
    scratch = manifest_path.with_name(f"{manifest_path.name}.{uuid4().hex}")
    try:
        with scratch.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, manifest_path)
    finally:
        scratch.unlink(missing_ok=True)


def _require_version_bump(old: PinnedArtifact, new: PinnedArtifact) -> None:
    """Refuse a replacement whose `version` lies about its weights.

    Segment-cache keys carry `version` (ADR 0003 §5): audio narrated with
    the old weights stays cached until the number moves. A re-pin that
    changes `source` or `files` under the same version would mix stale
    Segments with new ones mid-Narration, and a rollback would collide two
    different pin sets on one key. A Voice Reference digest is part of the
    Generation Record; its transcript
    remains pinned by Catalog version, so changes to the clip, transcript, or
    digest are held to the same version-bump rule.
    """
    if new.version < old.version:
        raise CurationError(
            f"{new.id} version {new.version} rolls back the committed "
            f"version {old.version}"
        )
    # Order-insensitive: a draft that merely reorders `files` names the same
    # bytes, and demanding a bump for it would invalidate every cached
    # Segment for weights that did not change — the exact damage this check
    # exists to prevent.
    if new.version == old.version and (
        new.source != old.source
        or sorted(new.files, key=lambda file: file.path)
        != sorted(old.files, key=lambda file: file.path)
        or (
            isinstance(old, CatalogEntry)
            and isinstance(new, CatalogEntry)
            and _references_changed(old, new)
        )
    ):
        raise CurationError(
            f"{new.id} changed its pinned bytes or the clip a Voice names: bump "
            f"version above {old.version}, or cached Segments from the old "
            f"weights will play alongside the new ones"
        )


def _references_changed(old: CatalogEntry, new: CatalogEntry) -> bool:
    """Whether a Voice both entries offer now names a different clip or
    transcript or digest. A Voice only one side has is not a swap: its Segments were
    never cached."""
    old_refs = {voice.id: voice.reference for voice in old.voices}
    new_refs = {voice.id: voice.reference for voice in new.voices}
    return any(
        old_refs[voice_id] != new_refs[voice_id]
        for voice_id in old_refs.keys() & new_refs.keys()
    )


def _sweep_orphaned_clips(published: list[Path]) -> None:
    """Delete the clips of Voices this entry used to offer and no longer does.

    Dropping or renaming a Voice would otherwise leave its `.m4a` in
    `public/previews/` forever — bundled into the app, referenced by nothing,
    and invisible to the test that checks every manifest `preview` exists.
    Every clip of one entry lives in one `<name>/<tag>/` directory, which
    holds because `Voice.id` is one path segment and `preview_relpath` spells
    the rest; an id carrying a `/` would put clips in parents this glob never
    looks in. `publish` refused any clip set that does not cover the entry's
    Voices, and the schema requires at least one Voice, so `published` is
    never empty, and the sweep is bounded to the entry being curated and can
    never reach another one's clips.
    """
    keep = set(published)
    for stale in published[0].parent.glob("*.m4a"):
        if stale not in keep:
            stale.unlink()


def publish_support(manifest_path: Path, entry: SupportModel) -> None:
    """Atomically publish a pinned Support Model without Voice Preview machinery."""
    _replace_manifest(manifest_path, _merged_manifest(manifest_path, entry))
