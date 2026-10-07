"""Engine startup confines Hugging Face to the Readily tree and disables its
telemetry (ADR 0003 §4, threat model egress inventory)."""

import os
from pathlib import Path

import pytest

from readily_engine.download.environment import (
    configure_hf_environment,
    default_data_dir,
)

HF_VARS = ("HF_HUB_DISABLE_TELEMETRY", "HF_HOME", "HF_HUB_DISABLE_PROGRESS_BARS")


@pytest.fixture(autouse=True)
def hf_env_restored():
    # configure_hf_environment mutates os.environ for real (that is its
    # job), so restore the pre-test values ourselves — monkeypatch can't
    # undo writes it didn't make.
    saved = {var: os.environ.get(var) for var in HF_VARS}
    for var in HF_VARS:
        os.environ.pop(var, None)
    yield
    for var, value in saved.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


def test_hf_telemetry_disabled(tmp_path: Path):
    configure_hf_environment(tmp_path)

    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_hf_metadata_confined_to_data_dir(tmp_path: Path):
    configure_hf_environment(tmp_path)

    hf_home = Path(os.environ["HF_HOME"])
    assert hf_home.is_relative_to(tmp_path)


def test_hf_progress_bars_disabled(tmp_path: Path):
    # Download progress reaches the UI over SSE; tqdm would only spam the
    # supervisor's stderr log.
    configure_hf_environment(tmp_path)

    assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"


def test_the_data_tree_is_the_one_the_shell_names(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("READILY_DATA_DIR", str(tmp_path / "Readily"))

    assert default_data_dir() == tmp_path / "Readily"


@pytest.mark.parametrize(
    ("platform", "below"),
    [("darwin", ("Library", "Application Support")), ("linux", (".local", "share"))],
)
def test_a_standalone_engine_keeps_its_data_where_the_platform_does(
    monkeypatch, tmp_path: Path, platform, below
):
    monkeypatch.delenv("READILY_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_data_dir(platform) == tmp_path.joinpath(*below, "Readily")
