"""The Voice Model qualification harness runs checks A-F over captured evidence."""

import struct
from pathlib import Path

import numpy as np
import pytest

from readily_engine.audio.qualification import main, qualify_corpus, qualify_run

CORPUS = Path(__file__).parent / "fixtures" / "audio" / "qualification"


def test_fixture_corpus_passes_every_analyzer_check():
    report = qualify_corpus(CORPUS)

    assert report.verdict == 0
    assert report.checks_run == frozenset("ABCDEF")
    assert report.discontinuities == 0
    assert report.crackle_events == 0
    assert {run.voice_model for run in report.runs} == {
        "kokoro:82m",
        "qwen3-tts:0.6b",
    }


def test_qualification_cli_emits_a_numeric_verdict(
    capsys: pytest.CaptureFixture[str],
):
    exit_code = main([str(CORPUS)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "voice_model=kokoro:82m" in output
    assert "voice_model=qwen3-tts:0.6b" in output
    assert output.rstrip().endswith("VERDICT: 0")


def test_checks_can_be_restricted_to_the_synthesis_subset(tmp_path):
    # A requalification capture has raw/processed Segments but no live
    # playback evidence (no tap, no callback log); restricting to the
    # synthesis checks must not count the missing evidence against it.
    import json
    import shutil

    source = CORPUS / "039-kokoro_82m"
    run = tmp_path / "requal" / "001-kokoro_82m"
    run.parent.mkdir()
    shutil.copytree(source, run)
    (run / "tap.wav").unlink()
    manifest = json.loads((run / "manifest.json").read_text())
    manifest.pop("cb_log")
    (run / "manifest.json").write_text(json.dumps(manifest))

    full = qualify_corpus(tmp_path / "requal")
    assert full.verdict > 0  # C and E correctly flag the missing evidence

    subset = qualify_corpus(tmp_path / "requal", checks=frozenset("ABDF"))
    assert subset.verdict == 0
    assert subset.checks_run == frozenset("ABDF")


@pytest.mark.parametrize("target", ["seg00-raw.wav", "seg00-processed.wav", "tap.wav"])
def test_non_finite_evidence_is_refused_not_qualified(tmp_path, target):
    # A NaN compares False against every threshold, and False is the passing
    # side of every check: a corpus carrying one must fail loudly, not
    # qualify with a clean verdict. All three wav evidence channels, because
    # the tap is guarded on its own branch.
    import shutil

    from readily_engine.audio.artifacts import read_wav

    run = tmp_path / "poisoned"
    shutil.copytree(CORPUS / "039-kokoro_82m", run)
    sample_rate, pcm = read_wav(run / target)
    poisoned = pcm.copy()
    poisoned[len(poisoned) // 2] = np.nan
    _write_wav(run / target, sample_rate, poisoned)

    with pytest.raises(ValueError, match="non-finite"):
        qualify_run(run)


def test_a_non_finite_callback_timestamp_is_refused_at_the_manifest(tmp_path):
    # Check E builds its late-callback intervals from `cb_log` floats, and a
    # NaN there sits on the passing side of the comparison. `RunManifest`
    # refuses it at parse — the same boundary the wav evidence crosses.
    import json
    import shutil

    from pydantic import ValidationError

    run = tmp_path / "poisoned"
    shutil.copytree(CORPUS / "039-kokoro_82m", run)
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["cb_log"][0][0] = float("nan")
    (run / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValidationError, match="finite"):
        qualify_run(run)


def test_the_playback_click_half_belongs_to_d_not_to_c(tmp_path):
    # D owns both click halves — clicks in the processed audio and clicks
    # playback added. Asking for C without D must therefore report no D
    # issues at all, and asking for D without C must still measure the
    # playback half against the tap.
    run = _run_with_a_click_in_the_tap(tmp_path)

    everything = qualify_run(run)
    playback_added = [
        issue for issue in everything.issues if "playback-added" in issue.detail
    ]
    assert [issue.check for issue in playback_added] == ["D"]

    without_d = qualify_run(run, checks=frozenset("ABCEF"))
    assert [issue for issue in without_d.issues if issue.check == "D"] == []
    assert without_d.discontinuities == 0
    # C still reports the tap disagreeing with the Segments, under C's name.
    assert any(issue.check == "C" for issue in without_d.issues)

    without_c = qualify_run(run, checks=frozenset("ABDEF"))
    assert [issue.detail for issue in without_c.issues if issue.check == "D"] == [
        issue.detail for issue in playback_added
    ]


def _run_with_a_click_in_the_tap(tmp_path: Path) -> Path:
    """A fixture run whose device-feed tap carries one isolated step the
    Segments do not — a click playback added, and nothing else."""
    import shutil

    from readily_engine.audio.artifacts import read_wav

    run = tmp_path / "clicked"
    shutil.copytree(CORPUS / "039-kokoro_82m", run)
    sample_rate, tap = read_wav(run / "tap.wav")
    quiet = 0.005 * sample_rate
    silent = next(
        index
        for index in range(int(quiet), len(tap) - int(quiet))
        if np.max(np.abs(tap[index - int(quiet) : index + int(quiet)])) < 0.01
    )
    clicked = tap.copy()
    clicked[silent] = 0.9
    _write_wav(run / "tap.wav", sample_rate, clicked)
    return run


def _write_wav(path: Path, sample_rate: int, pcm: np.ndarray) -> None:
    data = np.asarray(pcm, dtype="<f4").tobytes()
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        # IEEE float32, mono, at the tap's rate.
        + struct.pack("<IHHIIHH", 16, 3, 1, sample_rate, sample_rate * 4, 4, 32)
        + b"data"
        + struct.pack("<I", len(data))
    )
    path.write_bytes(header + data)


def test_qualification_cli_accepts_a_checks_subset(
    capsys: pytest.CaptureFixture[str],
):
    exit_code = main(["--checks", "ABDF", str(CORPUS)])

    assert exit_code == 0
    assert capsys.readouterr().out.rstrip().endswith("VERDICT: 0")
