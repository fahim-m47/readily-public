"""Fixtures every Engine test suite shares.

Two things lived in five and two copies respectively: the launch token and
its Authorization header, and a complete valid Catalog entry. Both are the
kind of literal that has to change everywhere at once — a new required field
on `CatalogEntry` used to mean editing two full entry literals — so they live
here once.
"""

import copy
import hashlib
import time
from pathlib import Path

from readily_engine.catalog import CatalogEntry, Source
from readily_engine.catalog.licences import licence_text
from readily_engine.curation import Draft

# A launch token of the right shape (32 hex characters, threat model B3).
TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Small files with real hashes, so store and download tests verify for real.
FILES = {
    "model.onnx": b"onnx bytes that stand in for weights",
    "voices/pack.bin": b"voice pack bytes",
}

# One complete, valid Catalog entry: every field `CatalogEntry` requires,
# and the shape a curation PR would write. It names a model and an
# Architecture no registry holds, so a test that needs a real Architecture
# has to say which one rather than inheriting Kokoro's by default.
ENTRY: dict[str, object] = {
    "name": "acme",
    "tag": "1m",
    "default_tag": True,
    "display_name": "Acme",
    "version": 1,
    "tier": "instant",
    "architecture": "fixture",
    "license": "Apache-2.0",
    "copyright_notice": None,
    "copyright_source": None,
    "ram_class_gb": 0.5,
    "provenance": "Byte-compared against the file qualified by ear.",
    "source": {"hf_repo": "acme/voices", "revision": "0" * 40},
    "attribution": None,
    "files": [{"path": "model.onnx", "sha256": "a" * 64, "size_bytes": 12}],
    "voices": [{"id": "narrator", "name": "Narrator", "language": "en-US"}],
    "default_voice": "narrator",
    "tunables": {
        "chunk_budget_chars": 450,
        "first_block_chars": 150,
        "pause_sentence_ms": 80,
        "pause_paragraph_break_ms": 400,
    },
}

# One complete, valid Manifest around it.
MANIFEST: dict[str, object] = {
    "schema_version": 1,
    "default_model": "acme",
    "models": [ENTRY],
}


def entry_dict(**overrides: object) -> dict[str, object]:
    """A copy of `ENTRY` with the given top-level fields replaced."""
    return {**copy.deepcopy(ENTRY), **overrides}


def make_entry(
    files: dict[str, bytes] | None = None, **overrides: object
) -> CatalogEntry:
    """A parsed entry. `files` replaces the file list with real SHA-256s of
    the given contents, so a store test can install what it declared."""
    if files is not None:
        overrides.setdefault(
            "files",
            [
                {
                    "path": path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
                for path, content in files.items()
            ],
        )
    return CatalogEntry.model_validate(entry_dict(**overrides))


def writing_fetch(files: dict[str, bytes]):
    """A `Fetch` that writes `files` into staging instead of downloading."""

    def fetch(entry: CatalogEntry, staging_dir: Path) -> None:
        del entry
        for path, content in files.items():
            target = staging_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    return fetch


def wait_until(predicate, timeout: float = 2.0) -> None:
    """Poll `predicate` until it holds, failing the test after `timeout`."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached")
        time.sleep(0.005)


def fetch_writing(files: dict[str, bytes]):
    """A curation `FetchFiles` that writes bytes instead of downloading."""

    def fetch(source: Source, paths: list[str], staging_dir: Path) -> None:
        del source
        # Like huggingface_hub, a path the repo does not have downloads
        # nothing rather than failing: pinning asks for licence files that
        # may or may not ship.
        for path in paths:
            if path not in files:
                continue
            target = staging_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[path])

    return fetch


ACMES_NOTICE = "Copyright (c) 2024 Acme"
MIT_WITH_A_HOLDER = (ACMES_NOTICE + "\n" + licence_text("MIT")).encode()


def draft_for(**overrides: object) -> Draft:
    """A curation Draft of the shared entry, with `overrides` applied."""
    return Draft.from_entry(make_entry(files=FILES, **overrides))
