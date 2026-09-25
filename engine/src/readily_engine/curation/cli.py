"""`readily-curate` — the one command that adds or re-pins a Voice Model.

    readily-curate kokoro:82m --write     # re-curate a committed entry
    readily-curate drafts/new.json --write  # add one

With `--write` the entry is merged into `catalog/manifest.json` and its
clips land in the bundled preview root; without it, nothing in the
repository is touched — the entry is printed for inspection and the clips
stay staged beside the capture, ready to audition. Re-curating an entry whose
files and audio have not changed rewrites the same bytes, so

    readily-curate kokoro:82m --write && git diff --exit-code catalog/manifest.json

is the regeneration proof: a hash that no longer matches what was pinned by
hand shows up as a diff, and a model that no longer qualifies never gets that
far.
"""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from readily_engine.catalog import MANIFEST_PATH, load_manifest
from readily_engine.curation.continuity import check_recipe
from readily_engine.curation.draft import CurationError, Draft, SupportDraft
from readily_engine.curation.entry import CURATION_CHECKS, curate
from readily_engine.curation.merge import publish, publish_support
from readily_engine.curation.support import curate_support
from readily_engine.download.environment import (
    configure_hf_environment,
    default_data_dir,
)
from readily_engine.loading import synthesizer_for
from readily_engine.loading.parameters import ParameterMismatch, check_parameters
from readily_engine.loading.registry import UnknownArchitecture, architecture_named
from readily_engine.store import ModelStore, VerificationError

# The bundled preview root Vite copies into the app (docs/voice-previews.md).
PREVIEW_ROOT = MANIFEST_PATH.parents[1] / "public" / "previews"


def _draft_for(target: str, manifest_path: Path) -> Draft | SupportDraft:
    """A `name:tag` names a committed entry; anything else is a draft file."""
    candidate = Path(target)
    if candidate.suffix == ".json":
        document = json.loads(candidate.read_text(encoding="utf-8"))
        return (
            SupportDraft if document.get("kind") == "forced-aligner" else Draft
        ).model_validate(document)
    catalog = load_manifest(manifest_path)
    support = next(
        (entry for entry in catalog.support_models if entry.id == target), None
    )
    if support is not None:
        return SupportDraft.from_entry(support)
    entry = catalog.resolve(target)
    if entry is None:
        raise CurationError(f"{target} is not in {manifest_path}")
    return Draft.from_entry(entry)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target", help="a committed entry's name:tag, or a path to a draft .json"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="merge the curated entry into the Catalog Manifest",
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--preview-root", type=Path, default=PREVIEW_ROOT)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="model store to download and promote into (default: curation's own)",
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        default=None,
        help="where to keep the qualification capture (default: under the data dir)",
    )
    parser.add_argument(
        "--check-simple",
        action="store_true",
        help="check the complete Simple-mode recipe without re-pinning files",
    )
    parser.add_argument(
        "--voice", help="check this Voice only; otherwise check every Voice"
    )
    parser.add_argument(
        "--chunk-budget",
        type=int,
        help="candidate Block budget for Simple-mode qualification, for an "
        "Architecture that sweeps one",
    )
    args = parser.parse_args(argv)
    if args.check_simple and args.write:
        parser.error("--check-simple keeps evidence for review; it does not publish")
    if not args.check_simple and (args.voice or args.chunk_budget is not None):
        parser.error("--voice and --chunk-budget require --check-simple")

    # Curation's own tree, not the app's. `curate` promotes the bytes it just
    # pinned over whatever an earlier curation left, and qualifies only after
    # that — so pointing this at the app's store would let a refused model
    # leave unqualified weights promoted under the entry's identity, with the
    # qualified ones already purged. The app re-checks no hash at load time
    # and keys its Segment cache on `version`, so it would then narrate with
    # weights its manifest never pinned and mix them with cached audio from
    # the old ones. A separate root costs one re-download and cannot.
    data_dir = args.data_dir or default_data_dir() / "curation" / "store"
    # Before anything imports huggingface_hub: telemetry off, cache confined.
    configure_hf_environment(data_dir)
    capture_dir = args.capture_dir or data_dir / "capture"

    try:
        if args.check_simple:
            return _check_simple(args, data_dir, capture_dir)
        draft = _draft_for(args.target, args.manifest)
        if isinstance(draft, Draft):
            # The Manifest parser cannot know whether the id resolves, or
            # whether the Architecture reads the knobs the Draft sets
            # (`catalog/` imports nothing from `loading/`); refuse here,
            # before a byte downloads and before `--write` commits it.
            check_parameters(architecture_named(draft.architecture).parameters, draft)
        if isinstance(draft, SupportDraft):
            support = curate_support(draft, ModelStore(data_dir))
            print(
                f"load checked {support.id}; "
                "onset qualification is a separate listening check"
            )
            if args.write:
                publish_support(args.manifest, support)
                print(f"wrote {support.id} to {args.manifest}")
            else:
                print(json.dumps(support.model_dump(mode="json"), indent=2))
            return 0
        curated = curate(
            draft,
            store=ModelStore(data_dir),
            capture_root=capture_dir,
        )
        print(f"capture {curated.capture_dir}")
        checks = "".join(sorted(CURATION_CHECKS))
        print(f"qualified {curated.entry.id} ({checks}) verdict=0")
        if args.write:
            for clip in publish(args.manifest, args.preview_root, curated):
                print(f"preview {clip}")
            print(f"wrote {curated.entry.id} to {args.manifest}")
        else:
            for clip in curated.clips.values():
                print(f"preview {clip} (staged)")
            print(json.dumps(curated.entry.model_dump(mode="json"), indent=2))
    # `ValidationError` because the draft is hand-written JSON: a curator who
    # mistypes a field deserves "refused", not a pydantic traceback.
    except (
        CurationError,
        ParameterMismatch,
        UnknownArchitecture,
        VerificationError,
        ValidationError,
    ) as error:
        print(f"refused: {error}")
        return 1
    return 0


def _check_simple(args: argparse.Namespace, data_dir: Path, capture_dir: Path) -> int:
    """Run the acoustic recipe check for every requested Voice; 1 if any fails."""
    entry = load_manifest(args.manifest).resolve(args.target)
    if entry is None:
        raise CurationError("Simple qualification requires a committed Catalog entry")
    candidates = architecture_named(entry.architecture).chunk_budget_candidates
    if args.chunk_budget is not None and candidates is None:
        raise CurationError(
            f"{entry.id}'s Architecture qualifies at one budget; "
            "--chunk-budget sweeps nothing"
        )
    # Range-check before composing: `compose` checks the control's hard range
    # and raises a bare ValueError, which would surface as a traceback.
    budget = entry.tunables.chunk_budget_chars
    if args.chunk_budget is not None:
        budget = args.chunk_budget
    if candidates is not None and budget not in candidates:
        raise CurationError(
            f"{entry.id} Simple qualification requires a "
            f"{candidates[0]} to {candidates[-1]} character budget"
        )
    if args.chunk_budget is not None:
        entry = entry.compose({"chunk_budget_chars": budget}).entry
    voices = [
        voice for voice in entry.voices if args.voice is None or voice.id == args.voice
    ]
    if not voices:
        raise CurationError("the entry does not offer that Voice")
    store = ModelStore(data_dir)
    if not store.installed(entry):
        raise CurationError(
            "install the pinned entry in this store before checking its recipe"
        )
    store.verify_installed(entry)
    synth = synthesizer_for(entry, store)
    passed = True
    for voice in voices:
        report = check_recipe(entry, voice.id, synth, capture_dir / voice.id)
        print(
            json.dumps(
                {
                    "voice": voice.id,
                    "recipe_sha256": report.recipe_sha256,
                    "failures": report.failures,
                }
            )
        )
        passed = passed and report.passed
    if not passed:
        return 1
    print(
        "Acoustic checks passed. Listen to every joined.wav before "
        "qualifying the recipe in the Catalog. This check does not qualify it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
