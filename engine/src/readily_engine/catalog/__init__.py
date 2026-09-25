"""The Catalog: the curated list of Voice Models Readily can download and run.

This package parses the baked-in Catalog Manifest and answers questions about
it. It decides *what* the Engine is allowed to fetch and from where — the
repos, revisions and SHA-256s that `download/` and `store/` then enforce — so
it is part of trust boundary B1 even though it touches neither network nor
weights. It is offline and pure by construction.
"""

from readily_engine.catalog.licence_table import LICENCE_OBLIGATIONS, LicenceObligations
from readily_engine.catalog.licences import licence_text, licence_text_for
from readily_engine.catalog.manifest import (
    BROKEN_MARKER,
    MANIFEST_PATH,
    REFERENCE_ROOT,
    SCHEMA_VERSION,
    CatalogEntry,
    DerivedFile,
    EntryEditorial,
    LicenceAttribution,
    Manifest,
    ManifestFile,
    PausePolicy,
    PinnedArtifact,
    Source,
    SupportModel,
    Tier,
    Tunables,
    Voice,
    VoiceIdentity,
    VoiceReference,
    load_manifest,
    model_file_path,
    preview_clip_path,
    reference_clip_path,
)

__all__ = [
    "BROKEN_MARKER",
    "LICENCE_OBLIGATIONS",
    "MANIFEST_PATH",
    "REFERENCE_ROOT",
    "SCHEMA_VERSION",
    "CatalogEntry",
    "DerivedFile",
    "EntryEditorial",
    "LicenceAttribution",
    "LicenceObligations",
    "Manifest",
    "ManifestFile",
    "PausePolicy",
    "PinnedArtifact",
    "Source",
    "SupportModel",
    "Tier",
    "Tunables",
    "Voice",
    "VoiceIdentity",
    "VoiceReference",
    "licence_text",
    "licence_text_for",
    "load_manifest",
    "model_file_path",
    "preview_clip_path",
    "reference_clip_path",
]
