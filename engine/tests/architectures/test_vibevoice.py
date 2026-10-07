"""VibeVoice-Realtime's Architecture: what is unique to it is the preset Voice
it names by id, the curly quotes it straightens, its 7.5 Hz latent budget,
and the tokenizer it bundles in place of mlx-audio's Hub fetch."""

import shutil

import pytest
from conformance import Conformance
from fakes import FakeMlx, assert_parameters_reach_the_upstream_call, mlx_model_dir
from generation_fakes import record

from readily_engine.loading import vibevoice

model_dir = pytest.fixture(mlx_model_dir)


class TestConformance(Conformance):
    model_id = "vibevoice-realtime:0.5b"
    sample_rate = None
    tokens_per_second = vibevoice.LATENTS_PER_SECOND


def upstream_call(generation):
    calls = []

    class Model:
        def generate(self, text, **kwargs):
            calls.append((text, kwargs))
            return iter(())

    list(vibevoice.generate(Model(), generation))
    return calls[0]


def test_the_preset_voice_is_named_and_curly_quotes_are_straightened():
    text, kwargs = upstream_call(
        record("\u201cIt\u2019s \u2018fine\u2019,\u201d she said.", "en-Emma_woman")
    )

    assert text == "\"It's 'fine',\" she said."
    assert kwargs["voice"] == "en-Emma_woman"
    assert kwargs["max_tokens"] == vibevoice.token_budget(text)


def test_parameters_reach_the_upstream_call():
    assert_parameters_reach_the_upstream_call(
        vibevoice.generate, {"cfg_scale": 1.5, "ddpm_steps": 5}
    )


def test_the_bundled_tokenizer_is_attached_without_the_hub(model_dir, monkeypatch):
    mlx = FakeMlx()
    mlx.install(monkeypatch)

    model = vibevoice._load(model_dir)

    assert mlx.loaded == [model_dir]
    assert mlx.tokenizers == [vibevoice.TOKENIZER_DIRECTORY]
    assert model.tokenizer == f"tokenizer from {vibevoice.TOKENIZER_DIRECTORY}"


def test_an_altered_bundled_tokenizer_is_refused(tmp_path, monkeypatch):
    altered = shutil.copytree(vibevoice.TOKENIZER_DIRECTORY, tmp_path / "tokenizer")
    with (altered / "merges.txt").open("a") as merges:
        merges.write("Ġ z\n")
    monkeypatch.setattr(vibevoice, "TOKENIZER_DIRECTORY", altered)

    with pytest.raises(ValueError, match=r"merges\.txt"):
        vibevoice.verify_bundled_tokenizer()


def test_a_stray_file_beside_the_bundled_tokenizer_is_refused(tmp_path, monkeypatch):
    altered = shutil.copytree(vibevoice.TOKENIZER_DIRECTORY, tmp_path / "tokenizer")
    (altered / "tokenizer.json").write_text("{}")
    monkeypatch.setattr(vibevoice, "TOKENIZER_DIRECTORY", altered)

    with pytest.raises(ValueError, match=r"tokenizer\.json"):
        vibevoice.verify_bundled_tokenizer()


def test_a_finder_file_beside_the_bundled_tokenizer_is_ignored(tmp_path, monkeypatch):
    altered = shutil.copytree(vibevoice.TOKENIZER_DIRECTORY, tmp_path / "tokenizer")
    (altered / ".DS_Store").write_bytes(b"\0")
    monkeypatch.setattr(vibevoice, "TOKENIZER_DIRECTORY", altered)

    vibevoice.verify_bundled_tokenizer()
