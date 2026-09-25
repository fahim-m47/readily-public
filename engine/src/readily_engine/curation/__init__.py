"""Curation: the scripted step that lets a model into the Catalog.

ADR 0003 leaves curation a script, not a runtime: whenever a Voice Model
enters the Catalog or bumps its weights, its pinned hashes and its Voice
Preview clips have to be regenerated, and doing that by hand is how a wrong
hash ships. This package is that script — pin, install, capture, qualify,
emit — and it runs on a developer's machine, never in the app.

It is a *composition* of boundaries rather than a new one: the network comes
from `download/`, verification and promotion from `store/`, inference from
`loading/` (so a capture runs against a promoted, hash-verified directory
like everything else), encoding from `audio/encoding.py`, and the verdict
from `audio/qualification.py`. Nothing here imports a downloader, a model
loader or a subprocess of its own, which is what keeps the Semgrep rules on
those packages meaningful (engine/README.md § "Layout is a security
boundary").
"""

from readily_engine.curation.capture import (
    CAPTURE_PASSAGE,
    capture_voice,
    dress_for_audition,
    preview_relpath,
)
from readily_engine.curation.draft import CurationError, Draft
from readily_engine.curation.entry import CuratedEntry, curate
from readily_engine.curation.merge import publish
from readily_engine.curation.pinning import pin

__all__ = [
    "CAPTURE_PASSAGE",
    "CuratedEntry",
    "CurationError",
    "Draft",
    "capture_voice",
    "curate",
    "dress_for_audition",
    "pin",
    "preview_relpath",
    "publish",
]
