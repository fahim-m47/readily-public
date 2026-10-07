"""Content-addressed Segments: identical speech is stored once.

FLAC on macOS and WAV elsewhere (ADR 0015); the store names each file by
the codec it was built with.
"""

import hashlib
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from readily_engine.audio import FloatPcm
from readily_engine.audio.encoding import (
    AudioEncodingError,
    Encoder,
    encode_flac,
    encode_wav,
)
from readily_engine.timings import Timing

logger = logging.getLogger(__name__)

_SEGMENT_HASH = re.compile(r"^[0-9a-f]{64}$")
# Inode, size, mtime and the sidecar's hash: what has to hold still for a
# remembered digest to still describe the file.
_FileSignature = tuple[int, int, int, str]
Decoder = Callable[[Path], "StoredAudio"]


class SegmentStorageError(AudioEncodingError):
    """A Segment could not be stored as a complete audio file."""


@dataclass(frozen=True)
class StoredAudio:
    pcm: FloatPcm
    sample_rate: int
    timings: tuple[Timing, ...] = ()


class SegmentMetadata(BaseModel):
    """The `.frames` sidecar beside each Segment's audio file.

    Legacy sidecars hold a bare frame count; `SegmentStore._metadata` lifts
    those into this shape with no hash and no words.
    """

    model_config = ConfigDict(extra="forbid")

    frame_count: int = Field(ge=0, strict=True)
    audio_sha256: str | None = None
    timings: tuple[Timing, ...] = ()


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def decode_flac(path: Path) -> StoredAudio:
    """Decode a stored FLAC to float32 mono PCM at its native sample rate."""
    import miniaudio

    decoded = miniaudio.flac_read_file_f32(str(path))
    return StoredAudio(
        pcm=np.asarray(decoded.samples, dtype=np.float32).copy(),
        sample_rate=int(decoded.sample_rate),
    )


def decode_wav(path: Path) -> StoredAudio:
    """Decode a stored WAV to float32 mono PCM at its native sample rate."""
    import miniaudio

    decoded = miniaudio.wav_read_file_f32(str(path))
    return StoredAudio(
        pcm=np.asarray(decoded.samples, dtype=np.float32).copy(),
        sample_rate=int(decoded.sample_rate),
    )


@dataclass(frozen=True)
class SegmentCodec:
    """How a store writes and reads its audio files, and what it names them.

    One value so the three always agree: a file is only ever decoded by the
    codec whose suffix it carries.
    """

    suffix: str
    encode: Encoder
    decode: Decoder


FLAC_SEGMENTS = SegmentCodec("flac", encode_flac, decode_flac)
WAV_SEGMENTS = SegmentCodec("wav", encode_wav, decode_wav)


def segment_codec(platform: str = sys.platform) -> SegmentCodec:
    """FLAC where afconvert can encode it, WAV everywhere else (ADR 0015)."""
    return FLAC_SEGMENTS if platform == "darwin" else WAV_SEGMENTS


def _fsync(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SegmentStore:
    """Hash-sharded audio files with crash-safe publication. `codec` is this
    platform's unless a test pins one."""

    def __init__(self, root: Path, *, codec: SegmentCodec | None = None) -> None:
        self.root = root
        self._codec = codec or segment_codec()
        # Keys whose audio file last digested to its sidecar's hash, remembered by
        # the file the digest was taken of. One playback asks whether a Block
        # is verified several times over; the bytes are read once.
        self._verified: dict[str, _FileSignature] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._sweep_temporaries()

    def path_for(self, key: str) -> Path:
        if _SEGMENT_HASH.fullmatch(key) is None:
            raise ValueError("Segment keys must be 64 lowercase hexadecimal characters")
        return self.root / key[:2] / f"{key}.{self._codec.suffix}"

    def _frames_path(self, key: str) -> Path:
        return self.path_for(key).with_name(f"{key}.frames")

    def has(self, key: str) -> bool:
        """Whether both files exist, without checking their contents.

        Both files or neither: the audio alone may decode to the encoder's
        padding rather than the Block, so a lone audio file is a miss. Callers
        that only want presence ask here instead of guessing at paths.
        """
        return self.path_for(key).is_file() and self._frames_path(key).is_file()

    def has_verified(self, key: str) -> bool:
        """Whether the sidecar vouches for the audio bytes, without decoding.

        Digests the audio file the first time it is asked about a given file and
        answers from a `stat` after that.
        """
        return self._verified_metadata(key) is not None

    def _metadata(self, key: str) -> SegmentMetadata | None:
        """Parse the sidecar once; a missing or unreadable pair is a miss."""
        if not self.has(key):
            return None
        try:
            raw = json.loads(self._frames_path(key).read_text())
            if type(raw) is int:
                return SegmentMetadata(frame_count=raw)
            return SegmentMetadata.model_validate(raw)
        except (OSError, ValueError):
            logger.warning("Stored Segment metadata could not be read: %s", key)
            return None

    def _verified_metadata(self, key: str) -> SegmentMetadata | None:
        """Digestless legacy pairs are misses until synthesis replaces them."""
        metadata = self._metadata(key)
        if metadata is None or metadata.audio_sha256 is None:
            return None
        path = self.path_for(key)
        try:
            stat = path.stat()
            signature = (
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                metadata.audio_sha256,
            )
            if self._verified.get(key) == signature:
                return metadata
            if metadata.audio_sha256 == _digest(path):
                self._verified[key] = signature
                return metadata
        except OSError:
            logger.warning("Stored Segment audio could not be read: %s", key)
        self._verified.pop(key, None)
        return None

    def read(self, key: str) -> StoredAudio | None:
        """Decode the Segment, refusing a waveform its sidecar does not vouch for.

        The audio file is digested once before decoding; a replacement waveform
        under the same key therefore cannot inherit the old words.
        """
        metadata = self._verified_metadata(key)
        if metadata is None:
            return None
        path = self.path_for(key)
        try:
            stored = self._codec.decode(path)
        except Exception:
            logger.exception("Stored Segment could not be decoded")
            return None
        if metadata.frame_count > len(stored.pcm):
            return None
        return StoredAudio(
            stored.pcm[: metadata.frame_count], stored.sample_rate, metadata.timings
        )

    def timings(self, key: str, sample_rate: int) -> tuple[Timing, ...]:
        """Read word metadata without decoding or digesting the PCM.

        The sidecar is trusted here: `read` is where the waveform is checked
        against it, and playback goes through `read`.
        """
        metadata = self._metadata(key)
        if metadata is None:
            return ()
        duration = metadata.frame_count / sample_rate
        return tuple(
            timing for timing in metadata.timings if timing.end_sec <= duration
        )

    def stored_keys(self) -> frozenset[str]:
        """Keys with at least one on-disk artifact owned by this store."""
        keys: set[str] = set()
        for _directory, _names, files in os.walk(self.root):
            for name in files:
                key, separator, suffix = name.partition(".")
                if (
                    separator
                    and suffix in {self._codec.suffix, "frames"}
                    and _SEGMENT_HASH.fullmatch(key) is not None
                ):
                    keys.add(key)
        return frozenset(keys)

    def disk_usage(self) -> int:
        """Bytes occupied by Segment artifacts, including incomplete pairs."""
        return sum(self.disk_usage_for(key) for key in self.stored_keys())

    def disk_usage_for(self, key: str) -> int:
        """Bytes this Segment's artifacts occupy, or 0 for what is not there.

        A file that vanishes between the check and the `stat` is counted as
        gone rather than raised: measuring the store is not serialized
        against evicting from it, so a sweep or a delete running alongside
        a Settings read would otherwise surface as an error.
        """
        total = 0
        for path in (self.path_for(key), self._frames_path(key)):
            with suppress(OSError):
                total += path.stat().st_size
        return total

    def delete(self, key: str) -> int:
        """Remove one Segment pair and return the bytes it occupied."""
        paths = (self.path_for(key), self._frames_path(key))
        self._verified.pop(key, None)
        removed = self.disk_usage_for(key)
        for path in paths:
            path.unlink(missing_ok=True)
        shard = self.path_for(key).parent
        with suppress(OSError):
            shard.rmdir()
        return removed

    def write(
        self,
        key: str,
        pcm: FloatPcm,
        sample_rate: int,
        *,
        timings: tuple[Timing, ...] = (),
    ) -> StoredAudio:
        existing = self.read(key)
        if existing is not None:
            if timings and not existing.timings:
                self._publish_frames(
                    key,
                    SegmentMetadata(
                        frame_count=len(existing.pcm),
                        audio_sha256=_digest(self.path_for(key)),
                        timings=timings,
                    ),
                )
                return StoredAudio(existing.pcm, existing.sample_rate, timings)
            return existing
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        temporary = destination.parent / f".{token}.tmp.{self._codec.suffix}"
        # afconvert's input sits beside its output; for a WAV Segment it is
        # the same file.
        intermediate = destination.parent / f".{token}.tmp.wav"
        source = np.asarray(pcm, dtype=np.float32)
        try:
            try:
                self._codec.encode(source, sample_rate, temporary)
            except SegmentStorageError:
                raise
            except Exception as error:
                raise SegmentStorageError("The Segment could not be encoded") from error
            if not temporary.is_file():
                raise SegmentStorageError("The Segment could not be encoded")
            _fsync(temporary)
            self._publish_frames(
                key,
                SegmentMetadata(
                    frame_count=len(source),
                    audio_sha256=_digest(temporary),
                    timings=timings,
                ),
            )
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
            self._verified.pop(key, None)
        finally:
            temporary.unlink(missing_ok=True)
            intermediate.unlink(missing_ok=True)
        stored = self.read(key)
        if stored is None:
            raise SegmentStorageError("The stored Segment could not be decoded")
        return stored

    def _publish_frames(self, key: str, metadata: SegmentMetadata) -> None:
        """Replace the sidecar atomically; the pair is complete once the audio lands."""
        destination = self._frames_path(key)
        temporary = destination.parent / f".{uuid.uuid4().hex}.tmp.frames"
        try:
            temporary.write_text(metadata.model_dump_json())
            _fsync(temporary)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _sweep_temporaries(self) -> None:
        for directory, _names, files in os.walk(self.root):
            for name in files:
                if name.startswith(".") and name.endswith(
                    (".tmp.wav", f".tmp.{self._codec.suffix}", ".tmp.frames")
                ):
                    Path(directory, name).unlink(missing_ok=True)
