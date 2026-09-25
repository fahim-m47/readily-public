"""Checks A-F for qualifying Voice Models against captured PCM runs."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from readily_engine.audio import FloatPcm
from readily_engine.audio.artifacts import (
    HF8K_THRESHOLD,
    compare_feed,
    find_discontinuities,
    find_noise_bursts,
    hf8k_ratio,
    read_wav,
)

Check = Literal["A", "B", "C", "D", "E", "F"]
ALL_CHECKS = frozenset(cast(Check, value) for value in "ABCDEF")

# A NaN lands on the passing side of every analyzer comparison, so evidence
# floats are refused at parse — the same boundary the wav files cross in
# `_load_run`.
_Finite = Annotated[float, Field(allow_inf_nan=False)]


class SegmentManifest(BaseModel):
    """One captured Segment's metadata, as the capture harness writes it."""

    model_config = ConfigDict(frozen=True)

    i: int
    sr: int
    audio_len: int


class CapturedSegment(SegmentManifest):
    """One Segment as `curation/`'s capture harness records it: the fields
    the analyzer reads, plus descriptive detail no check consumes — kept for
    a human diagnosing a refusal."""

    start: int
    pause_len: int
    raw_len: int
    raw_peak: float
    text_chars: int
    text: str


def write_run_manifest(
    run_dir: Path,
    *,
    model: str,
    voice: str,
    stream_sr: int,
    segments: Sequence[CapturedSegment],
) -> None:
    """Write `manifest.json` for one captured run.

    The writer half of the protocol `RunManifest` parses, kept beside the
    reader so the two cannot drift: a capture written here is one
    `qualify_run` can read, by type rather than by convention.
    """
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "model": model,
                "voice": voice,
                "stream_sr": stream_sr,
                "segments": [segment.model_dump() for segment in segments],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class RunManifest(BaseModel):
    """`manifest.json` for one captured Narration.

    Parsed rather than indexed-and-cast: a capture whose shape has drifted
    should fail here with the field that is wrong, not somewhere downstream
    with a number that happened to be a string. Unknown keys are allowed —
    the harness is free to record extra debug detail — but every field the
    analyzer reads has to be present and of the right type.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model: str
    segments: tuple[SegmentManifest, ...]
    # Absent on single-rate captures, where the Segments' own rate is the
    # stream rate.
    stream_sr: int | None = None
    cb_log: tuple[tuple[_Finite, _Finite, int], ...] = ()


@dataclass(frozen=True)
class QualificationIssue:
    """One failed analyzer check."""

    check: Check
    detail: str


@dataclass(frozen=True)
class SegmentEvidence:
    """The captured Voice Model and Engine PCM for one Segment."""

    index: int
    sample_rate: int
    audio_length: int
    raw: FloatPcm
    processed: FloatPcm


@dataclass(frozen=True)
class RunReport:
    """Numeric qualification result for one captured Narration."""

    path: Path
    voice_model: str
    issues: tuple[QualificationIssue, ...]
    discontinuities: int
    crackle_events: int
    checks_run: frozenset[Check] = ALL_CHECKS

    @property
    def verdict(self) -> int:
        return len(self.issues)


@dataclass(frozen=True)
class CorpusReport:
    """Aggregate numeric verdict for a Voice Model fixture corpus."""

    runs: tuple[RunReport, ...]

    @property
    def verdict(self) -> int:
        return sum(run.verdict for run in self.runs)

    @property
    def checks_run(self) -> frozenset[Check]:
        return frozenset(check for run in self.runs for check in run.checks_run)

    @property
    def discontinuities(self) -> int:
        return sum(run.discontinuities for run in self.runs)

    @property
    def crackle_events(self) -> int:
        return sum(run.crackle_events for run in self.runs)


def _load_run(
    path: Path,
) -> tuple[RunManifest, tuple[SegmentEvidence, ...], FloatPcm | None]:
    manifest = RunManifest.model_validate_json((path / "manifest.json").read_text())
    segments: list[SegmentEvidence] = []
    for metadata in manifest.segments:
        stem = path / f"seg{metadata.i:02d}"
        raw_rate, raw = read_wav(stem.with_name(f"{stem.name}-raw.wav"))
        processed_rate, processed = read_wav(
            stem.with_name(f"{stem.name}-processed.wav")
        )
        if raw_rate != metadata.sr or processed_rate != metadata.sr:
            raise ValueError(
                f"Segment {metadata.i} sample rate disagrees with "
                f"{path / 'manifest.json'}"
            )
        if not (np.isfinite(raw).all() and np.isfinite(processed).all()):
            raise ValueError(f"Segment {metadata.i} in {path} has non-finite samples")
        segments.append(
            SegmentEvidence(
                index=metadata.i,
                sample_rate=metadata.sr,
                audio_length=metadata.audio_len,
                raw=raw,
                processed=processed,
            )
        )

    tap_path = path / "tap.wav"
    tap = None
    if tap_path.exists():
        tap_rate, tap = read_wav(tap_path)
        if tap_rate != _stream_rate(manifest, segments):
            raise ValueError(f"tap sample rate disagrees with {path / 'manifest.json'}")
        if not np.isfinite(tap).all():
            raise ValueError(f"device-feed tap in {path} has non-finite samples")
    return manifest, tuple(segments), tap


def _stream_rate(manifest: RunManifest, segments: Sequence[SegmentEvidence]) -> int:
    """The rate the device tap was captured at: the manifest's own, or the
    Segments' when the capture ran at a single rate."""
    return manifest.stream_sr or segments[0].sample_rate


def qualify_run(path: str | Path, checks: frozenset[Check] = ALL_CHECKS) -> RunReport:
    """Run the requested analyzer checks over one captured Narration
    directory. A requalification capture that has no live playback evidence
    (no tap, no callback log) restricts itself to the synthesis checks
    A/B/D/F instead of counting the missing evidence as failures."""

    run_path = Path(path)
    manifest, segments, tap = _load_run(run_path)
    voice_model = manifest.model
    issues: list[QualificationIssue] = []

    high_frequency_scores: list[float] = []
    for segment in segments if "A" in checks else ():
        played = segment.processed[: segment.audio_length]
        clipping = int(np.sum(np.abs(played) >= 0.999))
        dc_offset = float(np.mean(played)) if len(played) else 0.0
        score = hf8k_ratio(segment.raw, segment.sample_rate)
        high_frequency_scores.append(score)
        if clipping:
            issues.append(
                QualificationIssue(
                    "A", f"Segment {segment.index} clips at {clipping} samples"
                )
            )
        if abs(dc_offset) > 0.01:
            issues.append(
                QualificationIssue(
                    "A", f"Segment {segment.index} has DC offset {dc_offset:.4f}"
                )
            )
        if score > HF8K_THRESHOLD:
            issues.append(
                QualificationIssue(
                    "A", f"Segment {segment.index} has hf8k ratio {score:.4f}"
                )
            )
    if len(high_frequency_scores) > 1:
        later_average = sum(high_frequency_scores[1:]) / len(high_frequency_scores[1:])
        if (
            high_frequency_scores[0] > HF8K_THRESHOLD
            and high_frequency_scores[0] > 2.5 * later_average
        ):
            issues.append(
                QualificationIssue("A", "first Segment has a warmup noise artifact")
            )

    for segment in segments if "B" in checks else ():
        played = segment.processed[: segment.audio_length]
        if not len(played):
            continue
        start, end = abs(float(played[0])), abs(float(played[-1]))
        if start > 0.02 or end > 0.02:
            issues.append(
                QualificationIssue(
                    "B",
                    f"Segment {segment.index} has hard edges {start:.3f}/{end:.3f}",
                )
            )

    expected = np.concatenate([segment.processed for segment in segments])
    stream_rate = _stream_rate(manifest, segments)

    if "C" not in checks:
        pass
    elif tap is None or not len(tap):
        issues.append(QualificationIssue("C", "device-feed tap is missing"))
    else:
        inserted, mismatches, missing_samples = compare_feed(tap, expected)
        for position, length in inserted:
            if length > stream_rate * 0.002:
                issues.append(
                    QualificationIssue(
                        "C",
                        f"{length / stream_rate * 1000:.0f}ms starvation gap at "
                        f"{position / stream_rate:.2f}s",
                    )
                )
        if mismatches:
            issues.append(
                QualificationIssue("C", f"{mismatches} corrupted feed samples")
            )
        if missing_samples:
            issues.append(
                QualificationIssue(
                    "C", f"{missing_samples} expected samples were not played"
                )
            )

    expected_discontinuities = np.array([], dtype=int)
    extra_tap_discontinuities = 0
    if "D" in checks:
        expected_discontinuities = find_discontinuities(expected, stream_rate)
        if len(expected_discontinuities):
            issues.append(
                QualificationIssue(
                    "D",
                    f"{len(expected_discontinuities)} processed-audio discontinuities",
                )
            )
        if tap is not None and len(tap):
            extra_tap_discontinuities = max(
                0,
                len(find_discontinuities(tap, stream_rate))
                - len(expected_discontinuities),
            )
            if extra_tap_discontinuities:
                issues.append(
                    QualificationIssue(
                        "D",
                        f"{extra_tap_discontinuities} playback-added discontinuities",
                    )
                )

    callback_log = manifest.cb_log
    if "E" not in checks:
        pass
    elif len(callback_log) <= 2:
        issues.append(QualificationIssue("E", "callback timing evidence is missing"))
    else:
        timestamps = np.array([callback[0] for callback in callback_log], dtype=float)
        frames = np.array([callback[1] for callback in callback_log], dtype=float)
        filled = np.array([callback[2] for callback in callback_log], dtype=int)
        intervals = np.diff(timestamps)
        expected_intervals = frames[:-1] / stream_rate
        nonzero = np.flatnonzero(filled)
        audible = np.zeros(len(intervals), dtype=bool)
        empty_mid_narration = 0
        if len(nonzero):
            audible[nonzero[0] : nonzero[-1]] = True
            empty_mid_narration = int(np.sum(filled[nonzero[0] : nonzero[-1]] == 0))
        late = np.flatnonzero((intervals > expected_intervals * 2 + 0.002) & audible)
        if len(late):
            issues.append(QualificationIssue("E", f"{len(late)} late callbacks"))
        if empty_mid_narration:
            issues.append(
                QualificationIssue(
                    "E", f"{empty_mid_narration} empty callbacks mid-Narration"
                )
            )

    crackle_events = 0
    for segment in segments if "F" in checks else ():
        bursts = find_noise_bursts(segment.processed, segment.sample_rate)
        crackle_events += len(bursts)
        for burst in bursts:
            issues.append(
                QualificationIssue(
                    "F",
                    f"Segment {segment.index} crackle at "
                    f"{burst.start / segment.sample_rate:.2f}s",
                )
            )

    return RunReport(
        path=run_path,
        voice_model=voice_model,
        issues=tuple(issues),
        discontinuities=len(expected_discontinuities) + extra_tap_discontinuities,
        crackle_events=crackle_events,
        checks_run=checks,
    )


def qualify_corpus(
    path: str | Path, checks: frozenset[Check] = ALL_CHECKS
) -> CorpusReport:
    """Qualify every captured run below a fixture corpus directory."""

    corpus_path = Path(path)
    if (corpus_path / "manifest.json").is_file():
        run_paths = [corpus_path]
    else:
        run_paths = sorted(
            manifest.parent for manifest in corpus_path.rglob("manifest.json")
        )
    if not run_paths:
        raise ValueError(f"no qualification manifests below {corpus_path}")
    return CorpusReport(tuple(qualify_run(run_path, checks) for run_path in run_paths))


def main(argv: Sequence[str] | None = None) -> int:
    """Print per-run results and the aggregate numeric verdict."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument(
        "--checks",
        default="ABCDEF",
        help="which analyzer checks to run, e.g. ABDF for a capture with no "
        "live playback evidence",
    )
    args = parser.parse_args(argv)
    requested = frozenset(cast(Check, letter) for letter in args.checks.upper())
    unknown = requested - ALL_CHECKS
    if unknown:
        parser.error(f"unknown checks: {''.join(sorted(unknown))}")
    report = qualify_corpus(args.corpus, requested)
    for run in report.runs:
        print(f"{run.path.name}: voice_model={run.voice_model} verdict={run.verdict}")
        for issue in run.issues:
            print(f"  {issue.check}: {issue.detail}")
    print(f"VERDICT: {report.verdict}")
    return int(report.verdict != 0)


if __name__ == "__main__":
    raise SystemExit(main())
