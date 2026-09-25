"""Qualification refuses a walking Voice or clipped ending on the full passage."""

import numpy as np
import pytest

from readily_engine.catalog import load_manifest
from readily_engine.curation.continuity import check_recipe
from readily_engine.generation import GeneratedAudio


class PassageSynthesizer:
    def __init__(self, *, walking=False, clipped=False):
        self.calls = 0
        self.walking = walking
        self.clipped = clipped

    def generate(self, record):
        rate = 24000
        pitch = 140 + (self.calls * 3 if self.walking else 0)
        self.calls += 1
        pcm = (0.2 * np.sin(2 * np.pi * pitch * np.arange(rate) / rate)).astype(
            np.float32
        )
        if not self.clipped:
            pcm[-2400:] *= np.linspace(1, 0, 2400)
            pcm = np.pad(pcm, (1200, 1200))
        return GeneratedAudio(np.pad(pcm, (0, 2400)), rate)


@pytest.mark.parametrize("defect", ["walking", "clipped"])
def test_check_refuses_an_unqualified_recipe(tmp_path, defect):
    entry = load_manifest().resolve("kokoro:82m")
    report = check_recipe(
        entry, entry.default_voice, PassageSynthesizer(**{defect: True}), tmp_path
    )
    assert not report.passed
    assert any(defect in reason for reason in report.failures)
    assert len(report.blocks) >= 20


def test_check_keeps_complete_evidence_for_a_stable_recipe(tmp_path):
    entry = load_manifest().resolve("kokoro:82m")
    synth = PassageSynthesizer()
    report = check_recipe(entry, entry.default_voice, synth, tmp_path)
    assert report.passed
    assert synth.calls == len(report.blocks)
    assert len(report.blocks) >= 20
    assert (tmp_path / "qualification.json").is_file()
    assert (tmp_path / "joined.wav").is_file()


@pytest.mark.parametrize("defect", ["walking", "clipped"])
def test_curation_command_exits_unsuccessfully_for_bad_recipes(
    tmp_path, monkeypatch, defect
):
    from readily_engine.curation import cli

    monkeypatch.setattr(cli.ModelStore, "installed", lambda self, entry: True)
    monkeypatch.setattr(cli.ModelStore, "verify_installed", lambda self, entry: None)
    monkeypatch.setattr(
        cli,
        "synthesizer_for",
        lambda entry, store: PassageSynthesizer(**{defect: True}),
    )
    assert (
        cli.main(
            [
                "kokoro:82m",
                "--check-simple",
                "--voice",
                "af_heart",
                "--data-dir",
                str(tmp_path / "store"),
                "--capture-dir",
                str(tmp_path / "capture"),
            ]
        )
        == 1
    )


def test_qwen_qualification_requires_the_smaller_candidate_budget(tmp_path, capsys):
    from readily_engine.curation.cli import main

    assert main(["qwen3-tts:0.6b", "--check-simple", "--data-dir", str(tmp_path)]) == 1
    assert "200 to 250" in capsys.readouterr().out


def test_a_swept_budget_outside_the_candidates_is_refused_not_a_traceback(
    tmp_path, capsys
):
    # The Architecture's range is checked before the value reaches
    # `compose`, whose own hard-range check raises a bare ValueError.
    from readily_engine.curation.cli import main

    argv = ["qwen3-tts:0.6b", "--check-simple", "--chunk-budget", "0"]
    assert main([*argv, "--data-dir", str(tmp_path)]) == 1
    assert "200 to 250" in capsys.readouterr().out


def test_a_block_that_qualifies_at_one_budget_refuses_a_sweep(tmp_path, capsys):
    from readily_engine.curation.cli import main

    argv = ["kokoro:82m", "--check-simple", "--chunk-budget", "220"]
    assert main([*argv, "--data-dir", str(tmp_path)]) == 1
    assert "--chunk-budget sweeps nothing" in capsys.readouterr().out


def test_a_zero_budget_still_needs_check_simple(tmp_path, capsys):
    # argparse no longer bounds the value (the registry does, later), so a 0
    # must not read as "no budget given" and fall through to a real curation.
    from readily_engine.curation.cli import main

    argv = ["qwen3-tts:0.6b", "--chunk-budget", "0", "--data-dir", str(tmp_path)]
    with pytest.raises(SystemExit) as refused:
        main(argv)
    assert refused.value.code == 2
    assert "require --check-simple" in capsys.readouterr().err


def test_curation_refuses_stale_promoted_bytes_before_synthesis(tmp_path, monkeypatch):
    from readily_engine.curation import cli

    entry = load_manifest().resolve("kokoro:82m")
    store = cli.ModelStore(tmp_path / "store")
    promoted = store.promoted_dir(entry)
    promoted.mkdir(parents=True)
    stale = promoted / entry.files[0].path
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"old weights")

    def must_not_load(*args):
        pytest.fail("unverified bytes reached the Synthesizer")

    monkeypatch.setattr(cli, "synthesizer_for", must_not_load)
    assert (
        cli.main(
            [
                entry.id,
                "--check-simple",
                "--data-dir",
                str(tmp_path / "store"),
                "--capture-dir",
                str(tmp_path / "capture"),
            ]
        )
        == 1
    )
    assert stale.read_bytes() == b"old weights"
    assert not (tmp_path / "capture").exists()
