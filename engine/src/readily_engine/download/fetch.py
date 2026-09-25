"""The Engine's Voice Model download: the one sanctioned network call.

Fetches a Catalog entry's files — exactly the ones the manifest names,
never "the whole repo", so an upstream that gains a malicious extra file
gains nothing (threat model B1) — at the entry's immutable pinned revision,
into the store's staging directory. Resumable: an interrupted download's
partial files stay in staging and the next call continues them. This module
downloads only; verification and promotion belong to `readily_engine.store`.
"""

from collections.abc import Sequence
from pathlib import Path

from readily_engine.catalog import PinnedArtifact, Source, model_file_path


def fetch_entry(entry: PinnedArtifact, staging_dir: Path) -> None:
    """Download `entry`'s manifest-named files into `staging_dir`."""
    fetch_files(entry.source, [file.path for file in entry.files], staging_dir)


def fetch_files(source: Source, paths: Sequence[str], staging_dir: Path) -> None:
    """Download exactly `paths` from `source` into `staging_dir`.

    The named-paths half of `fetch_entry`, for the one caller that has no
    hashes yet: `curation/` downloads a draft's files in order to *compute*
    the pins the manifest will carry. Every path is re-validated here rather
    than trusted, so this entry point cannot widen the allow-patterns beyond
    what `ManifestFile` would have accepted (threat model B1).
    """
    # Imported at call time: `download.environment` must configure the HF
    # environment (telemetry off, metadata confined) before the client
    # loads, and huggingface_hub reads that environment at import.
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=source.hf_repo,
        revision=source.revision,
        token=False,
        local_dir=str(staging_dir),
        # Exact relative paths, not glob patterns: `model_file_path` keeps
        # them clean of wildcard characters.
        allow_patterns=[model_file_path(path) for path in paths],
    )
