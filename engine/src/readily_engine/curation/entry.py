"""One draft in, one qualified Catalog entry out — or nothing at all.

The order is the policy. Files are pinned from the bytes that arrived, the
model is installed through the real store (so curation loads from a promoted
directory like everything else does — threat model B1/B2), every Voice is
captured through the real Backend, and only once the analyzer returns a clean
verdict does anything become a manifest entry or an encoded clip. Nothing
here writes to the repository at all: the clips are staged beside the
capture, and `merge.publish` is the one explicit step that lands them and
the manifest row together — so a curation run for inspection mutates
nothing a release reads.

It does leave the model promoted, qualified or not, because it has to install
before it can listen. That is why `cli.py` points this at a store of
curation's own rather than the app's — a refused model sits in a tree only
this script reads.
"""

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from readily_engine.audio import FloatPcm
from readily_engine.audio.encoding import encode_m4a
from readily_engine.audio.qualification import Check, CorpusReport, qualify_corpus
from readily_engine.catalog import CatalogEntry
from readily_engine.curation.capture import (
    capture_voice,
    dress_for_audition,
    preview_relpath,
)
from readily_engine.curation.draft import CurationError, Draft
from readily_engine.curation.pinning import FetchFiles, pin
from readily_engine.download.fetch import fetch_files
from readily_engine.generation import Synthesizer
from readily_engine.loading import synthesizer_for
from readily_engine.store import ModelStore

# A curation capture is synthesis evidence, not playback evidence: nothing
# is fed to an audio device, so the two checks that measure the device feed
# (C) and the callback deadlines (E) have nothing to measure and would count
# their own absence as failures (`qualification.qualify_run`).
CURATION_CHECKS = frozenset(cast(Check, letter) for letter in "ABDF")

MakeSynthesizer = Callable[[CatalogEntry, ModelStore], Synthesizer]
Encoder = Callable[[FloatPcm, int, Path], None]


@dataclass(frozen=True)
class CuratedEntry:
    """A manifest entry that passed, and the evidence that it did.

    `clips` maps each Voice's manifest preview path to the staged clip a
    curator can audition before deciding to publish — `merge.publish` is
    what copies them into the bundled preview root.
    """

    entry: CatalogEntry
    report: CorpusReport
    capture_dir: Path
    clips: Mapping[str, Path]


def curate(
    draft: Draft,
    *,
    store: ModelStore,
    capture_root: Path,
    fetch: FetchFiles = fetch_files,
    make_synthesizer: MakeSynthesizer = synthesizer_for,
    encode: Encoder = encode_m4a,
    checks: frozenset[Check] = CURATION_CHECKS,
) -> CuratedEntry:
    """Pin, install, capture, qualify — and only then emit an entry."""
    files = pin(draft, store.staging_dir(draft.name, draft.tag), fetch)

    # Provisional only in that its Voices have no clips yet: its hashes are
    # final, which is what lets the store verify and promote it, and what
    # lets the capture below run against the same bytes the app will.
    provisional = draft.entry(files, {})
    relpaths = {
        voice.id: preview_relpath(provisional, voice.id) for voice in provisional.voices
    }

    store.promote_staged(provisional)

    synthesizer = make_synthesizer(provisional, store)
    capture_dir = capture_root / f"{draft.name}_{draft.tag}"
    # A stale run from an earlier attempt would be qualified alongside this
    # one and could pass a model on evidence it no longer produces.
    shutil.rmtree(capture_dir, ignore_errors=True)
    streams: dict[str, tuple[FloatPcm, int]] = {}
    for voice in provisional.voices:
        streams[voice.id] = capture_voice(
            provisional, voice.id, synthesizer, capture_dir / voice.id
        )

    report = qualify_corpus(capture_dir, checks)
    if report.verdict:
        raise CurationError(
            f"{provisional.id} did not qualify: {_issues(report)}\n"
            f"The capture is at {capture_dir}."
        )

    # Staged inside the capture directory, which this run owns and just
    # cleared: nothing lands in the bundled preview root until the curator
    # publishes, so inspecting a run mutates no file a release reads. The
    # leading underscore is outside `Voice.id`'s first-character class, so
    # no Voice's capture run directory can collide with this one.
    clips: dict[str, Path] = {}
    for voice_id, (stream, sample_rate) in streams.items():
        clip = capture_dir / "_previews" / f"{voice_id}.m4a"
        clip.parent.mkdir(parents=True, exist_ok=True)
        encode(dress_for_audition(stream, sample_rate), sample_rate, clip)
        clips[relpaths[voice_id]] = clip

    return CuratedEntry(
        entry=draft.entry(files, relpaths),
        report=report,
        capture_dir=capture_dir,
        clips=clips,
    )


def _issues(report: CorpusReport) -> str:
    return "; ".join(
        f"{run.path.name} {issue.check}: {issue.detail}"
        for run in report.runs
        for issue in run.issues
    )
