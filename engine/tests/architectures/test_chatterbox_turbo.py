"""Chatterbox Turbo's Architecture: text in, a stream out, the Voice baked into the
promoted directory (ADR 0009 amendment)."""

import pytest
from conformance import Conformance
from fakes import (
    FakeModel,
    assert_parameters_reach_the_upstream_call,
    clean_chunk,
    degenerate_chunk,
    mlx_model_dir,
    synthesizer,
)
from generation_fakes import record

from readily_engine.loading import chatterbox_turbo

model_dir = pytest.fixture(mlx_model_dir)


class TestConformance(Conformance):
    model_id = "chatterbox:turbo"
    sample_rate = None


class ChatterboxFakeModel(FakeModel):
    """Chatterbox Turbo's `generate` takes no voice or speed: the voice is
    baked into the model directory's `conds.safetensors`."""

    def generate(self, text, *, stream, streaming_interval):
        assert stream is True
        return self._replay({"text": text})


def test_chatterbox_turbo_is_asked_only_for_a_stream_and_still_gated(model_dir):
    model = ChatterboxFakeModel([[degenerate_chunk()], [clean_chunk()]])
    synth = synthesizer(model_dir, model, chatterbox_turbo.generate)

    with pytest.raises(RuntimeError, match="degenerate"):
        synth.generate(record("Hello there.", "default"))

    assert model.calls == [{"text": "Hello there."}]


def test_chatterbox_turbo_is_asked_for_text_and_nothing_else(model_dir):
    # The request boundary refuses speeds the entry cannot honour; a stored
    # one that reaches the lane anyway plays at the model's fixed pace
    # rather than failing the Block.
    model = ChatterboxFakeModel([[clean_chunk()]])
    synth = synthesizer(model_dir, model, chatterbox_turbo.generate)

    audio = synth.generate(record("Hello there.", "default")).pcm

    assert model.calls == [{"text": "Hello there."}]
    assert len(audio) == len(clean_chunk())


def test_parameters_reach_the_upstream_call():
    assert_parameters_reach_the_upstream_call(chatterbox_turbo.generate)
