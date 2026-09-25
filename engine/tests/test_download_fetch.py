"""The Engine downloads exactly the files the manifest names, at the pinned
revision (threat model B1) — never "the whole repo".

huggingface_hub is faked through `sys.modules`, so these tests exercise the
call the Engine would make without importing the real client or touching
the network.
"""

import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from conftest import FILES, make_entry

from readily_engine.download.fetch import fetch_entry
from readily_engine.loading import qwen3
from readily_engine.loading.mlx_lane import MlxSynthesizer


@pytest.fixture
def snapshot_calls(monkeypatch):
    calls: list[dict] = []
    fake_hub = types.ModuleType("huggingface_hub")

    def snapshot_download(**kwargs):
        calls.append(kwargs)

    fake_hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    return calls


def test_fetch_asks_for_exactly_the_manifest_files(snapshot_calls, tmp_path: Path):
    entry = make_entry(FILES)

    fetch_entry(entry, tmp_path / "staging")

    (call,) = snapshot_calls
    assert call["allow_patterns"] == ["model.onnx", "voices/pack.bin"]


def test_fetch_pins_the_immutable_revision(snapshot_calls, tmp_path: Path):
    entry = make_entry(FILES)

    fetch_entry(entry, tmp_path / "staging")

    (call,) = snapshot_calls
    assert call["repo_id"] == "acme/voices"
    assert call["revision"] == "0" * 40


def test_fetch_lands_in_the_given_staging_directory(snapshot_calls, tmp_path: Path):
    entry = make_entry(FILES)

    fetch_entry(entry, tmp_path / "staging")

    (call,) = snapshot_calls
    assert call["token"] is False
    assert call["local_dir"] == str(tmp_path / "staging")


def test_fetch_does_not_inherit_offline_mode_or_wait_for_a_model_load(
    monkeypatch, tmp_path: Path
):
    constants = SimpleNamespace(HF_HUB_OFFLINE=False)
    constants.is_offline_mode = lambda: constants.HF_HUB_OFFLINE
    fake_hub = types.ModuleType("huggingface_hub")
    fake_hub.constants = constants
    load_entered = Event()
    release_load = Event()
    download_started = Event()
    fetch_entered = Event()
    offline_values: list[bool] = []

    def snapshot_download(**_kwargs):
        offline_values.append(constants.is_offline_mode())
        fetch_entered.set()

    fake_hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}")

    def load(_path):
        load_entered.set()
        assert release_load.wait(timeout=1)
        return object()

    entry = make_entry(FILES)

    def download():
        download_started.set()
        fetch_entry(entry, tmp_path / "staging")

    with ThreadPoolExecutor(max_workers=2) as pool:
        loading = pool.submit(
            MlxSynthesizer,
            model_dir,
            generate=qwen3.generate,
            runaway_seconds=qwen3.runaway_seconds,
            model_factory=load,
        )
        assert load_entered.wait(timeout=1)
        downloading = pool.submit(download)
        assert download_started.wait(timeout=1)
        fetch_finished_while_loading = fetch_entered.wait(timeout=0.2)
        release_load.set()
        loading.result(timeout=1)
        downloading.result(timeout=1)

    assert fetch_finished_while_loading is True
    assert offline_values == [False]
