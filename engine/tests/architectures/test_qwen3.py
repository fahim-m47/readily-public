"""Qwen3-TTS's Architecture: a Voice conditions on its Voice Reference at every
Block, and the codec-token ceiling tracks the text's runaway budget."""

import numpy as np
import pytest
from conformance import Conformance
from fakes import (
    SAMPLE_RATE,
    FakeMlx,
    FakeModel,
    assert_parameters_reach_the_upstream_call,
    clean_chunk,
    mlx_model_dir,
    synthesizer,
    tone,
)
from generation_fakes import record

from readily_engine.loading import qwen3
from readily_engine.loading.references import VoiceReferenceAudio

model_dir = pytest.fixture(mlx_model_dir)


class TestConformance(Conformance):
    model_id = "qwen3-tts:0.6b"
    sample_rate = None


@pytest.fixture
def fake_mlx(monkeypatch):
    """What the lane does with `mlx.core.array` — hand a reference's samples
    over as one in-memory array — is what is asserted."""
    FakeMlx().install(monkeypatch)


def test_a_voice_with_a_reference_is_conditioned_on_it_at_every_block(
    model_dir, fake_mlx
):
    # The Base checkpoint's speaker table is empty, so a speaker name
    # draws a new voice per generation. With a reference, every Block gets
    # the same clip and transcript and never the name.
    model = FakeModel([[clean_chunk()], [clean_chunk()]])
    reference = VoiceReferenceAudio(tone(200, 0.5), "Ready to read.")
    lane = synthesizer(
        model_dir, model, qwen3.generate, references={("Chelsie", "a" * 64): reference}
    )

    lane.generate(record("First paragraph.", "Chelsie", reference_digest="a" * 64))
    lane.generate(record("Second paragraph.", "Chelsie", reference_digest="a" * 64))

    assert len(model.calls) == 2
    for call in model.calls:
        assert call["voice"] is None
        assert call["ref_text"] == "Ready to read."
        np.testing.assert_array_equal(call["ref_audio"], reference.pcm)


def test_a_checkpoint_without_the_encoder_is_refused_not_narrated_unconditioned(
    model_dir, fake_mlx
):
    # mlx-audio drops `ref_audio` silently when the speech tokenizer has no
    # encoder, which would put the per-paragraph voice reset back without a word.
    model = FakeModel([[clean_chunk()]], has_encoder=False)
    reference = VoiceReferenceAudio(tone(200, 0.5), "Ready to read.")
    lane = synthesizer(
        model_dir, model, qwen3.generate, references={("Chelsie", "a" * 64): reference}
    )

    with pytest.raises(RuntimeError, match="no speech-tokenizer encoder"):
        lane.generate(record("Hello.", "Chelsie", reference_digest="a" * 64))
    assert model.calls == []


def test_a_record_naming_an_unloaded_reference_says_which_one_is_missing(
    model_dir, fake_mlx
):
    model = FakeModel([[clean_chunk()]])
    reference = VoiceReferenceAudio(tone(200, 0.5), "Ready to read.")
    lane = synthesizer(
        model_dir, model, qwen3.generate, references={("Chelsie", "a" * 64): reference}
    )

    with pytest.raises(LookupError, match="'Chelsie'"):
        lane.generate(record("Hello.", "Chelsie", reference_digest="b" * 64))
    assert model.calls == []


def test_a_voice_without_a_reference_is_asked_for_by_name(model_dir, fake_mlx):
    model = FakeModel([[clean_chunk()]])
    reference = VoiceReferenceAudio(tone(200, 0.5), "Ready to read.")
    lane = synthesizer(
        model_dir, model, qwen3.generate, references={("Chelsie", "a" * 64): reference}
    )

    lane.generate(record("Hello.", "Ethan"))

    assert model.calls == [{"text": "Hello.", "voice": "Ethan", "speed": 1.0}]


@pytest.mark.parametrize("stream", [False, True])
def test_qwen_decode_mode_and_token_ceiling_apply_to_every_block(model_dir, stream):
    model = FakeModel([[clean_chunk(2.0)] * 10])
    lane = synthesizer(model_dir, model, qwen3.generate)

    audio = lane.generate(
        record("Hi.", "Chelsie", decode_mode="streaming" if stream else "non-streaming")
    ).pcm

    assert model.options == [(stream, 71)]
    assert len(audio) / SAMPLE_RATE <= 5.75


def test_a_longer_block_earns_a_higher_token_ceiling(model_dir):
    model = FakeModel([[clean_chunk(2.0)] * 20, [clean_chunk(2.0)] * 20])
    lane = synthesizer(model_dir, model, qwen3.generate)

    lane.generate(record("Hi.", "Chelsie", decode_mode="non-streaming"))
    lane.generate(record("Ready to read.", "Chelsie", decode_mode="non-streaming"))

    assert model.options == [(False, 71), (False, 106)]


def test_parameters_reach_the_upstream_call():
    assert_parameters_reach_the_upstream_call(qwen3.generate)


def test_the_block_conditions_on_a_reference_and_sweeps_the_simple_budgets():
    # Simple-mode qualification sweeps this range; `readily-curate
    # --chunk-budget` reads it from here.
    assert qwen3.ARCHITECTURE.conditioning == "reference"
    assert qwen3.ARCHITECTURE.chunk_budget_candidates == range(200, 251)
