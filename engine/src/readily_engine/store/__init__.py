"""The Engine-owned model store: staging, verification, atomic promote.

Invariant (ADR 0003): a promoted model directory exists ⇔ its contents are
hash-verified and complete. Files arrive in staging via
`readily_engine.download`, are checked against the Catalog Manifest's
SHA-256s, then atomically promoted. This package touches the filesystem
only — zero network.
"""

from readily_engine.store.downloads import DeleteOutcome, DownloadManager
from readily_engine.store.store import (
    Fetch,
    ModelStore,
    StoreUnwritable,
    VerificationError,
    file_sha256,
)

__all__ = [
    "DeleteOutcome",
    "DownloadManager",
    "Fetch",
    "ModelStore",
    "StoreUnwritable",
    "VerificationError",
    "file_sha256",
]
