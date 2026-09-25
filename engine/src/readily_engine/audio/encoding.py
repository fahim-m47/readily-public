"""The Engine's one home for turning float PCM into audio files.

Every `afconvert` invocation in the Engine lives here, so the container and
codec choices are reviewable in a single place: lossless FLAC for the Segment
cache, and — for Export — lossless WAV or AAC in an M4A. Never MP3.
"""

import logging
import struct
import subprocess  # nosemgrep: engine-no-process-spawn
import wave
from contextlib import suppress
from pathlib import Path

import numpy as np

from readily_engine.audio import FloatPcm

logger = logging.getLogger(__name__)

_PCM24_PEAK = 8_388_607
# Apple's FLAC encoder emits nothing below one packet; shorter PCM is padded
# for encode. The original frame count is written beside the FLAC so every
# read — not only write()'s return — trims back to the caller's length.
MIN_FLAC_FRAMES = 4608


class AudioEncodingError(RuntimeError):
    """PCM could not be written out as a complete audio file."""


def write_wav(path: Path, pcm: FloatPcm, sample_rate: int) -> None:
    """Write mono float PCM as a 24-bit WAV — the lossless Export format."""
    samples = np.clip(np.asarray(pcm, dtype=np.float64), -1.0, 1.0)
    scaled = np.rint(samples * _PCM24_PEAK).astype(np.int32)
    packed = np.empty(len(scaled) * 3, dtype=np.uint8)
    packed[0::3] = scaled & 0xFF
    packed[1::3] = (scaled >> 8) & 0xFF
    packed[2::3] = (scaled >> 16) & 0xFF
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(3)
        wav.setframerate(sample_rate)
        wav.writeframes(packed.tobytes())


def write_float_wav(path: Path, pcm: FloatPcm, sample_rate: int) -> None:
    """Write mono float PCM as a 32-bit IEEE-float WAV — the qualification
    capture format, which `audio.artifacts.read_wav` reads back sample for
    sample. Hand-assembled RIFF because `wave` writes integer PCM only, and
    quantizing a capture would make the analyzer measure the quantizer.
    """
    samples = np.asarray(pcm, dtype="<f4")
    data = samples.tobytes()
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,
        3,  # IEEE float
        1,  # mono
        sample_rate,
        sample_rate * 4,
        4,
        32,
        b"data",
        len(data),
    )
    path.write_bytes(header + data)


def encode_flac(pcm: FloatPcm, sample_rate: int, flac_path: Path) -> None:
    """Encode float PCM to FLAC via a 24-bit WAV and macOS afconvert."""
    samples = np.asarray(pcm, dtype=np.float32)
    if len(samples) < MIN_FLAC_FRAMES:
        padded = np.zeros(MIN_FLAC_FRAMES, dtype=np.float32)
        padded[: len(samples)] = samples
        samples = padded
    wav_path = flac_path.with_suffix(".wav")
    try:
        write_wav(wav_path, samples, sample_rate)
        # File format is the lowercase four-cc `flac`; `FLAC` is rejected as typ?.
        subprocess.run(
            [
                "/usr/bin/afconvert",
                str(wav_path),
                "-f",
                "flac",
                "-d",
                "flac",
                str(flac_path),
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError, wave.Error) as error:
        raise AudioEncodingError("The audio could not be encoded as FLAC") from error
    finally:
        wav_path.unlink(missing_ok=True)


def encode_m4a(pcm: FloatPcm, sample_rate: int, m4a_path: Path) -> None:
    """Encode float PCM to AAC in an M4A — the default Export format."""
    wav_path = m4a_path.with_suffix(f"{m4a_path.suffix}.wav")
    try:
        write_wav(wav_path, np.asarray(pcm, dtype=np.float32), sample_rate)
        subprocess.run(
            [
                "/usr/bin/afconvert",
                str(wav_path),
                "-f",
                "m4af",
                "-d",
                "aac",
                "-b",
                # afconvert rejects AAC bitrates above this for one channel
                # at 24 kHz, and accepts it at every rate a Voice Model
                # produces. Omitting -b falls back to roughly 16 kbps, which
                # is audibly worse than what was played in-app. A literal,
                # because the Semgrep allowlist pins this exact argv.
                "64000",
                str(m4a_path),
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError, wave.Error) as error:
        raise AudioEncodingError("The audio could not be encoded as M4A") from error
    finally:
        with suppress(OSError):
            wav_path.unlink(missing_ok=True)


def encode_wav(pcm: FloatPcm, sample_rate: int, wav_path: Path) -> None:
    """Write float PCM as a 24-bit WAV, matching `encode_flac`'s signature."""
    try:
        write_wav(wav_path, np.asarray(pcm, dtype=np.float32), sample_rate)
    except (OSError, wave.Error) as error:
        raise AudioEncodingError("The audio could not be written as WAV") from error
