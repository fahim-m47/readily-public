"""Voice Reference clips, verified and decoded once at Engine start.

Shared by every Architecture that conditions on a clip and by
`synthesizer_for`, which reads the clips eagerly so a bundled reference that
is missing or no longer matches its pinned SHA-256 fails at boot, before any
Narration and before anything decodes the bytes (threat model B2, ADR 0014).
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from readily_engine.audio import FloatPcm
from readily_engine.audio.artifacts import decode_wav
from readily_engine.catalog import CatalogEntry

# A reference clip is handed to the Voice Model unresampled, so it must
# already be at the model's own rate.
REFERENCE_SAMPLE_RATE = 24_000


@dataclass(frozen=True)
class VoiceReferenceAudio:
    """A decoded Voice Reference and its transcript."""

    pcm: FloatPcm
    text: str


type VoiceReferences = Mapping[tuple[str, str], VoiceReferenceAudio]

NO_REFERENCES: VoiceReferences = MappingProxyType({})


def load_reference(clip: Path, text: str) -> VoiceReferenceAudio:
    """Decode one bundled reference clip at the Voice Model's rate."""
    return _decode_reference(clip.read_bytes(), clip, text)


def _decode_reference(contents: bytes, clip: Path, text: str) -> VoiceReferenceAudio:
    rate, pcm = decode_wav(contents, clip)
    if rate != REFERENCE_SAMPLE_RATE:
        raise ValueError(
            f"{clip} is {rate} Hz; a reference clip is {REFERENCE_SAMPLE_RATE} Hz"
        )
    return VoiceReferenceAudio(pcm, text)


def voice_references(
    entry: CatalogEntry, root: Path
) -> dict[tuple[str, str], VoiceReferenceAudio]:
    """Verify and decode an entry's Voice References, keyed by Voice id and digest."""
    references = {}
    for voice in entry.voices:
        if voice.reference is None:
            continue
        clip = root / voice.reference.clip
        contents = clip.read_bytes()
        if hashlib.sha256(contents).hexdigest() != voice.reference.sha256:
            raise ValueError(f"Voice Reference SHA-256 mismatch: {clip}")
        references[voice.id, voice.reference.sha256] = _decode_reference(
            contents, clip, voice.reference.text
        )
    return references
