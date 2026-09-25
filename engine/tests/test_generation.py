"""A Segment's identity records every input that can change its audio."""

from dataclasses import replace

import numpy as np
import pytest
from conftest import make_entry

from readily_engine.generation import GeneratedAudio, GenerationRecord

RECORD = GenerationRecord(
    model_id="qwen3-tts:0.6b",
    catalog_version=4,
    voice_id="Chelsie",
    reference_digest="a" * 64,
    decode_mode="non-streaming",
    parameters={"temperature": 0.9, "top_k": 50},
    text="Hello.",
)


@pytest.mark.parametrize(
    "changes",
    [
        {"reference_digest": "b" * 64},
        {"seed": 42},
        {"parameters": {"temperature": 0.8, "top_k": 50}},
        {"text": "Goodbye."},
        {"catalog_version": 5},
        {"decode_mode": "streaming"},
    ],
)
def test_every_generation_input_changes_identity(changes):
    assert replace(RECORD, **changes).key != RECORD.key


def test_record_round_trip_preserves_identity_and_content_derived_seed():
    restored = GenerationRecord.from_json(RECORD.canonical_json())
    assert restored == RECORD
    assert restored.key == RECORD.key
    assert restored.rng_seed == RECORD.rng_seed
    assert replace(RECORD, seed=42).rng_seed == 42


def test_a_records_parameters_are_its_own_not_its_entrys():
    entry = make_entry(generation_parameters={"temperature": 0.9})
    record = GenerationRecord.for_entry(entry, entry.default_voice, "Hello.")
    record.parameters["temperature"] = 0.1
    assert entry.generation_parameters == {"temperature": 0.9}


def test_a_record_names_the_voice_its_entry_does_not_offer():
    with pytest.raises(ValueError, match="af_ghost"):
        GenerationRecord.for_entry(make_entry(), "af_ghost", "Hello.")


def test_a_records_text_is_canonicalised_so_callers_need_not_be():
    entry = make_entry()
    record = GenerationRecord.for_entry(entry, entry.default_voice, "  Hello.\n")
    assert record.text == "Hello."


def test_audio_at_an_impossible_sample_rate_is_refused():
    with pytest.raises(ValueError, match="sample rate"):
        GeneratedAudio(np.zeros(4, dtype=np.float32), 0)


def test_audio_with_no_samples_is_refused():
    with pytest.raises(ValueError, match="empty"):
        GeneratedAudio(np.zeros(0, dtype=np.float32), 24000)


def test_audio_carrying_a_non_finite_sample_is_refused():
    pcm = np.zeros(4, dtype=np.float32)
    pcm[2] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        GeneratedAudio(pcm, 24000)
