"""Cut the cloning entries' Voice Reference clips from consented corpora.

Run with `uv run --project engine/tools --extra clips engine/tools/reference_clips.py
engine/tools/reference_voices.json`. Needs ffmpeg on PATH; never imported by
the Engine.

The spec names each Voice's take: a VCTK speaker, microphone and one to four
consecutive utterances, or a Hi-Fi TTS shard and file. Only those members
are fetched — VCTK by HTTP range requests into the 11 GB datashare zip,
Hi-Fi TTS from the parquet shard that holds the row — and cached under
`--cache`. Each take is decoded to 24 kHz mono, trimmed to speech, joined
with short gaps, peak-normalised and scored; the same bytes land under
`catalog/references/<name>/<tag>/<Voice>.wav` for every cloning entry. The
score table and the draft Voice stanzas (clip, transcript, credit) print at
the end, ready to paste into a draft for `readily-curate`.
"""

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import wave
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CATALOG = Path(__file__).resolve().parents[2] / "catalog"

SAMPLE_RATE = 24_000
GAP_S = 0.45
FIT_S = (7.5, 10.0)
TRIM_FLOOR_DB = -45.0
TRIM_PAD_S = 0.08
# The level of the entry's own Chelsie clip. A cloning model reproduces its
# reference's loudness: at 0.7 the Qwen3 0.6B narrations peaked near full
# scale and tripped the curation gate's clipping and discontinuity checks,
# while at this level every Voice lands in the same range Chelsie does.
PEAK = 0.35
MAX_RUN = 4
# How a clip ends, matching the bundled Chelsie clip: the talker imitates its
# reference's ending, so the last word is faded out over the trim pad and
# followed by silence, and the clip is a whole number of 12.5 Hz codec tokens
# (`engine/tests/test_catalog_manifest.py` holds every committed clip to this).
TAIL_S = 0.36
CODEC_TOKENS_PER_SECOND = 12.5
TOKEN_SAMPLES = int(SAMPLE_RATE / CODEC_TOKENS_PER_SECOND)

# VCTK is an immutable DataShare deposit. The Hi-Fi TTS mirror is a git
# repository, so the script pins the commit it cut the clips at and checks
# every shard it downloads against the digest that commit records; a later
# commit must not re-cut a different take under the same Voice.
VCTK_ZIP = "https://datashare.ed.ac.uk/bitstream/handle/10283/3443/VCTK-Corpus-0.92.zip"
HIFI_REVISION = "f0a934ddf4e8da0fc6c53fb42fda2c9ac8ed93df"
HIFI_TREE = (
    f"https://huggingface.co/api/datasets/MikhailT/hifi-tts/tree/{HIFI_REVISION}/data"
)
HIFI_FILE = (
    f"https://huggingface.co/datasets/MikhailT/hifi-tts/resolve/{HIFI_REVISION}/data/"
)

# Credits per corpus, as the Catalog's `ReferenceAttribution` wants them.
# Hi-Fi TTS clips credit the LibriVox reader the spec names.
CORPUS_CREDIT = {
    "vctk": {
        "license": "CC-BY-4.0",
        "creator": "Junichi Yamagishi, Christophe Veaux and Kirsten MacDonald, "
        "The Centre for Speech Technology Research (CSTR), University of Edinburgh",
        "copyright_notice": "Copyright 2019 University of Edinburgh",
        "source": "https://datashare.ed.ac.uk/handle/10283/3443",
        "modified": True,
    },
    "hifi-tts": {
        "license": "CC-BY-4.0",
        "creator": "{reader}, reading for LibriVox; Hi-Fi TTS by NVIDIA",
        "copyright_notice": "Copyright 2021 NVIDIA Corporation",
        "source": "https://www.openslr.org/109/",
        "modified": True,
    },
}

# What a reader stumbles on and the Voice Models mispronounce: digits,
# brackets, quotes and colons. A transcript carrying one needs a different
# take, not an edit by hand.
_UNSPEAKABLE = re.compile(r'[\d()\[\]{}"“”:]')


def clean_transcript(raw: str) -> str:
    """The corpus line as it is read aloud: one line, no space before
    punctuation, no trailing comma, ending in a full stop."""
    text = " ".join(raw.split())
    text = re.sub(r"\s+([,.?!;])", r"\1", text).rstrip(",; ")
    if not text.endswith((".", "?", "!")):
        text += "."
    if _UNSPEAKABLE.search(text):
        raise ValueError(f"transcript needs a different take, not an edit: {text!r}")
    return text


def take_members(
    speaker: str, utterances: Sequence[int], mic: str
) -> list[tuple[str, str]]:
    """The (transcript, audio) member pairs of one VCTK take."""
    if not 1 <= len(utterances) <= MAX_RUN or list(utterances) != list(
        range(utterances[0], utterances[0] + len(utterances))
    ):
        raise ValueError(
            f"a take is one to {MAX_RUN} consecutive utterances, not {list(utterances)}"
        )
    return [
        (
            f"txt/{speaker}/{speaker}_{n:03d}.txt",
            f"wav48_silence_trimmed/{speaker}/{speaker}_{n:03d}_{mic}.flac",
        )
        for n in utterances
    ]


def trim_to_speech(
    samples: np.ndarray,
    sample_rate: int,
    floor_db: float = TRIM_FLOOR_DB,
    pad_s: float = TRIM_PAD_S,
) -> np.ndarray:
    """Cut the silence either side of the take, keeping a short pad."""
    envelope = np.abs(samples)
    threshold = 10 ** (floor_db / 20) * float(envelope.max())
    loud = np.flatnonzero(envelope > threshold)
    if threshold == 0 or not len(loud):
        raise ValueError("the take is silent")
    pad = int(pad_s * sample_rate)
    return samples[max(loud[0] - pad, 0) : loud[-1] + pad]


def join_with_gaps(
    pieces: Sequence[np.ndarray], sample_rate: int, gap_s: float = GAP_S
) -> np.ndarray:
    """One clip from the trimmed takes of consecutive sentences, a pause apart."""
    gap = np.zeros(int(gap_s * sample_rate), dtype=np.float32)
    joined = [piece for take in pieces for piece in (gap, take)]
    return np.concatenate(joined[1:])


def normalise_peak(samples: np.ndarray, peak: float = PEAK) -> np.ndarray:
    """Scale the clip so its loudest sample sits at `peak`."""
    return samples / float(np.abs(samples).max()) * peak


def finish_tail(
    samples: np.ndarray,
    sample_rate: int,
    fade_s: float = TRIM_PAD_S,
    tail_s: float = TAIL_S,
    token_samples: int = TOKEN_SAMPLES,
) -> np.ndarray:
    """Fade the clip's last `fade_s` into `tail_s` of silence, then pad to a
    whole number of codec tokens."""
    out = samples.astype(np.float32).copy()
    fade = min(int(fade_s * sample_rate), len(out))
    if fade > 1:
        ramp = 0.5 + 0.5 * np.cos(np.pi * np.arange(fade) / fade)
        out[-fade:] *= ramp.astype(np.float32)
    length = len(out) + int(tail_s * sample_rate)
    length += -length % token_samples
    return np.concatenate([out, np.zeros(length - len(out), dtype=np.float32)])


@dataclass(frozen=True)
class Score:
    """What a curator checks before a clip ships: how long it is, how quiet
    the room was, and how far the speech sits above it."""

    duration_s: float
    noise_floor_db: float
    snr_db: float

    @property
    def fit(self) -> str:
        low, high = FIT_S
        if self.duration_s < low:
            return "short"
        return "long" if self.duration_s > high else "ok"


def score(samples: np.ndarray, sample_rate: int) -> Score:
    """Measure a cut clip over 20 ms frames: the noise floor is the 5th
    percentile of frame RMS and the speech level the 90th, as the issue that
    chose each take scored it, so a second mic or take compares like for like."""
    window = int(0.02 * sample_rate)
    frames = samples[: len(samples) // window * window].reshape(-1, window)
    frames = frames[np.any(frames != 0, axis=1)]  # padded silence is not room
    rms_db = 20 * np.log10(np.sqrt(np.mean(frames**2, axis=1)) + 1e-9)
    floor, speech = np.percentile(rms_db, [5, 90])
    return Score(len(samples) / sample_rate, float(floor), float(speech - floor))


def cloning_entry_dirs(manifest_path: Path = CATALOG / "manifest.json") -> list[Path]:
    """Where every entry whose Voices clone a reference keeps its clips."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [
        manifest_path.parent / "references" / entry["name"] / entry["tag"]
        for entry in manifest["models"]
        if any(voice.get("reference") for voice in entry["voices"])
    ]


class RangeFile(io.RawIOBase):
    """A remote file read through HTTP range requests, so `zipfile` can
    pull single members out of an archive it never downloads whole."""

    def __init__(self, url: str) -> None:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        ) as response:
            self.url = response.geturl()
            self.size = int(response.headers["Content-Range"].split("/")[1])
        self.pos = 0

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        end = min(self.pos + len(buffer), self.size) - 1
        if end < self.pos:
            return 0
        request = urllib.request.Request(
            self.url, headers={"Range": f"bytes={self.pos}-{end}"}
        )
        with urllib.request.urlopen(request) as response:
            data = response.read()
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)


class Corpora:
    """Fetches corpus members on demand and keeps them under the cache."""

    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self._vctk: zipfile.ZipFile | None = None

    def vctk_member(self, member: str) -> bytes:
        target = self.cache / "vctk" / member
        if not target.exists():
            if self._vctk is None:
                print("opening the VCTK zip's directory over HTTP ranges", flush=True)
                self._vctk = zipfile.ZipFile(
                    io.BufferedReader(RangeFile(VCTK_ZIP), buffer_size=1 << 20)
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self._vctk.read(member))
        return target.read_bytes()

    def hifi_row(self, shard: str, file: str) -> tuple[bytes, str]:
        """The FLAC bytes and unprocessed transcript of one Hi-Fi TTS row."""
        import pyarrow.compute as pc
        import pyarrow.parquet as pq

        for name, sha256 in self._hifi_shards(shard):
            target = self.cache / "hifi-tts" / name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                print(f"downloading {name}", flush=True)
                with urllib.request.urlopen(HIFI_FILE + name) as response:
                    target.write_bytes(response.read())
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
                raise ValueError(
                    f"{target} is not the shard revision {HIFI_REVISION} records: "
                    "delete it from the cache and run again"
                )
            rows = pq.read_table(target, filters=pc.field("file") == file).to_pylist()
            if rows:
                return rows[0]["audio"]["bytes"], rows[0]["text_no_preprocessing"]
        raise ValueError(f"{file} is not in the {shard} shards of Hi-Fi TTS")

    def _hifi_shards(self, shard: str) -> list[tuple[str, str]]:
        """The split's parquet shards at the pinned revision, each with the
        SHA-256 the mirror's LFS pointer records for it."""
        with urllib.request.urlopen(HIFI_TREE) as response:
            tree = json.load(response)
        return sorted(
            (Path(item["path"]).name, item["lfs"]["oid"])
            for item in tree
            if Path(item["path"]).name.startswith(f"{shard}-")
        )


def decode(flac: bytes) -> np.ndarray:
    """The take as 24 kHz mono float samples, through ffmpeg's resampler
    with the long windowed-sinc filter (no soxr)."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-ac",
            "1",
            "-af",
            f"aresample={SAMPLE_RATE}:filter_size=256:cutoff=0.97",
            "-f",
            "f32le",
            "pipe:1",
        ],
        input=flac,
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype=np.float32)


def write_wav(path: Path, samples: np.ndarray) -> None:
    """Write the clip as the 16-bit mono WAV the Catalog pins by digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(np.round(samples * 32767), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm.tobytes())


@dataclass(frozen=True)
class Clip:
    """One cut Voice Reference: what `write_wav` writes, what `stanza` prints."""

    voice: str
    language: str
    samples: np.ndarray
    text: str
    attribution: dict[str, object]

    def stanza(self, target: Path) -> dict[str, object]:
        """The draft Voice for the entry whose clips live in `target`."""
        return {
            "id": self.voice,
            "name": self.voice,
            "language": self.language,
            "reference": {
                "clip": (
                    target.relative_to(target.parents[1]) / f"{self.voice}.wav"
                ).as_posix(),
                "text": self.text,
                "attribution": self.attribution,
            },
        }


def cut(spec: dict, corpora: Corpora) -> Clip:
    """One Voice's clip, from the take its spec names."""
    credit = dict(CORPUS_CREDIT[spec["corpus"]])
    if spec["corpus"] == "vctk":
        members = take_members(spec["speaker"], spec["utterances"], spec["mic"])
        texts = [corpora.vctk_member(txt).decode("utf-8") for txt, _ in members]
        takes = [decode(corpora.vctk_member(flac)) for _, flac in members]
    else:
        flac, text = corpora.hifi_row(spec["shard"], spec["file"])
        credit["creator"] = str(credit["creator"]).format(reader=spec["reader"])
        texts, takes = [text], [decode(flac)]
    trimmed = [trim_to_speech(take, SAMPLE_RATE) for take in takes]
    samples = normalise_peak(join_with_gaps(trimmed, SAMPLE_RATE))
    samples = finish_tail(samples, SAMPLE_RATE)
    text = " ".join(clean_transcript(line) for line in texts)
    return Clip(spec["voice"], spec["language"], samples, text, credit)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="the JSON file naming each take")
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(tempfile.gettempdir()) / "readily-reference-clips",
        help="where fetched corpus members are kept between runs",
    )
    parser.add_argument(
        "--only", action="append", default=[], help="cut only this Voice (repeatable)"
    )
    args = parser.parse_args(argv)
    voices: Iterable[dict] = json.loads(args.spec.read_text(encoding="utf-8"))["voices"]
    corpora = Corpora(args.cache)
    targets = cloning_entry_dirs()
    clips = [
        cut(spec, corpora)
        for spec in voices
        if not args.only or spec["voice"] in args.only
    ]
    print(f"{'Voice':<10}{'seconds':>8}{'floor dB':>10}{'SNR dB':>8}  fit")
    for clip in clips:
        result = score(clip.samples, SAMPLE_RATE)
        print(
            f"{clip.voice:<10}{result.duration_s:>8.2f}{result.noise_floor_db:>10.1f}"
            f"{result.snr_db:>8.1f}  {result.fit}"
        )
        for target in targets:
            write_wav(target / f"{clip.voice}.wav", clip.samples)
    print(
        f"\nwritten under {', '.join(str(t.relative_to(CATALOG)) for t in targets)}\n"
    )
    json.dump(
        {
            str(target.relative_to(target.parents[1])): [
                clip.stanza(target) for clip in clips
            ]
            for target in targets
        },
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    print()


if __name__ == "__main__":
    main()
