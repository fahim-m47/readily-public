"""VoxCPM2's Architecture: every Block is cloned from the Voice's reference,
encoded at the audio VAE's rate without mlx-audio's SciPy resample."""

from types import SimpleNamespace

import numpy as np
import pytest
from conformance import Conformance
from fakes import (
    SAMPLE_RATE,
    FakeMlx,
    FakeModel,
    clean_chunk,
    mlx_model_dir,
    synthesizer,
    tone,
)
from generation_fakes import record

from readily_engine.loading import voxcpm2
from readily_engine.loading.references import VoiceReferenceAudio

model_dir = pytest.fixture(mlx_model_dir)

DIGEST = "a" * 64


class TestConformance(Conformance):
    model_id = "voxcpm2:2b"
    sample_rate = None
    tokens_per_second = voxcpm2.PATCHES_PER_SECOND


def lane(model_dir, model):
    reference = VoiceReferenceAudio(tone(200, 1.5), "Ready to read.")
    return synthesizer(
        model_dir,
        model,
        voxcpm2.generate,
        voxcpm2.runaway_seconds,
        references={("Chelsie", DIGEST): reference},
    )


def test_every_block_is_cloned_from_the_reference_at_the_encoder_rate(model_dir):
    model = FakeModel([[clean_chunk()], [clean_chunk()]])
    narrator = lane(model_dir, model)

    narrator.generate(record("First paragraph.", "Chelsie", reference_digest=DIGEST))
    narrator.generate(record("Second paragraph.", "Chelsie", reference_digest=DIGEST))

    assert len(model.calls) == 2
    for call in model.calls:
        # 1.5s of 24 kHz reference, handed over as 16 kHz samples, never a path.
        assert isinstance(call["ref_audio"], np.ndarray)
        assert len(call["ref_audio"]) == 24_000
        assert call["voice"] is None


def test_a_voice_without_a_reference_is_refused(model_dir):
    model = FakeModel([[clean_chunk()]])

    with pytest.raises(ValueError, match="no Voice Reference"):
        lane(model_dir, model).generate(record("Hello.", "Chelsie"))
    assert model.calls == []


def test_a_longer_block_earns_a_higher_patch_ceiling(model_dir):
    model = FakeModel(
        [[clean_chunk(2.0)] * 20], tokens_per_second=voxcpm2.PATCHES_PER_SECOND
    )
    narrator = lane(model_dir, model)

    audios = [
        narrator.generate(
            record(
                text, "Chelsie", reference_digest=DIGEST, decode_mode="non-streaming"
            )
        ).pcm
        for text in ("Hi.", "Ready to read.")
    ]

    assert [ceiling for _stream, ceiling in model.options] == [35, 53]
    assert len(audios[1]) == pytest.approx(
        53 / voxcpm2.PATCHES_PER_SECOND * SAMPLE_RATE
    )


def test_the_diffusion_knobs_reach_the_upstream_call():
    calls = []

    class Model:
        def generate(self, text, **kwargs):
            calls.append(kwargs)
            return iter(())

    knobs = {"cfg_value": 2.5, "inference_timesteps": 6}
    reference = VoiceReferenceAudio(tone(200, 0.5), "Ready to read.")

    list(
        voxcpm2.generate(Model(), record("Hi.", "Chelsie", parameters=knobs), reference)
    )

    assert calls[0].items() >= knobs.items()


def test_every_block_continues_from_the_reference_and_its_transcript():
    calls = []

    class Model:
        def generate(self, text, **kwargs):
            calls.append((text, kwargs))
            return iter(())

    reference = VoiceReferenceAudio(tone(200, 0.5), "Ready to read.")
    text = "First line,\n  then\tthe second."

    list(voxcpm2.generate(Model(), record(text, "Chelsie"), reference))

    [(sent, kwargs)] = calls
    assert sent == "First line, then the second."
    assert kwargs["prompt_text"] == "Ready to read."
    assert kwargs["prompt_audio"] is kwargs["ref_audio"]


def test_loading_swaps_the_scipy_resample_for_the_engines_encoder(
    model_dir, monkeypatch
):
    # mlx-audio's own `_encode_wav` resamples every array reference with
    # SciPy (ADR 0006); the loaded model must encode it as handed over:
    # padded to whole patches, VAE-encoded, grouped into patches.
    fake = FakeMlx()
    fake.install(monkeypatch)
    encoded = []

    def encode(audio, rate):
        encoded.append((np.asarray(audio).shape, rate))
        return np.zeros((1, np.asarray(audio).shape[-1] // 640, 64))

    fake.model.patch_size = 4
    fake.model.audio_vae = SimpleNamespace(chunk_size=640, encode=encode)

    model = voxcpm2._load(model_dir)
    patches = model._encode_wav(np.ones(3_000, dtype=np.float32), padding_mode="right")

    # 3000 samples pad to one 2560-sample patch boundary above: 5120, which
    # the 640-sample hop encodes as eight frames, or two four-frame patches.
    assert encoded == [((1, 1, 5_120), voxcpm2.ENCODER_SAMPLE_RATE)]
    assert patches.shape == (2, 4, 64)
