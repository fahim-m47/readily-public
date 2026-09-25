"""Voice Reference clips: decoded once at boot, in the one shape the model
reads, after their SHA-256 has been checked against the Catalog."""

import hashlib

import numpy as np
import pytest
from conftest import make_entry
from fakes import SAMPLE_RATE, tone, write_wav

from readily_engine.loading.references import load_reference, voice_references


def test_load_reference_decodes_a_24k_mono_wav_to_float(tmp_path):
    clip = tmp_path / "Chelsie.wav"
    write_wav(clip, tone(200, 0.1))

    reference = load_reference(clip, "Ready.")

    assert reference.text == "Ready."
    assert reference.pcm.dtype == np.float32
    assert len(reference.pcm) == int(0.1 * SAMPLE_RATE)
    assert np.abs(reference.pcm).max() == pytest.approx(0.3, abs=1e-3)


@pytest.mark.parametrize(
    ("shape", "reason"),
    [({"rate": 22_050}, "24000 Hz"), ({"channels": 2}, "mono")],
    ids=["rate", "stereo"],
)
def test_load_reference_refuses_a_clip_the_model_would_misread(tmp_path, shape, reason):
    # The lane hands the samples over unresampled, so a clip at the wrong
    # rate conditions on the wrong voice rather than failing.
    clip = tmp_path / "Chelsie.wav"
    write_wav(clip, tone(200, 0.1), **shape)

    with pytest.raises(ValueError, match=reason):
        load_reference(clip, "Ready.")


@pytest.mark.parametrize("tampered", [False, True])
def test_voice_references_verifies_every_clip_before_loading(tmp_path, tampered):
    write_wav(tmp_path / "qwen3-tts/0.6b/Chelsie.wav", tone(200, 0.1))
    clip = tmp_path / "qwen3-tts/0.6b/Chelsie.wav"
    digest = hashlib.sha256(clip.read_bytes()).hexdigest()
    entry = make_entry(
        name="qwen3-tts",
        tag="0.6b",
        architecture="qwen3",
        voices=[
            {
                "id": "Chelsie",
                "name": "Chelsie",
                "language": "en-US",
                "reference": {
                    "clip": "qwen3-tts/0.6b/Chelsie.wav",
                    "text": "Hi.",
                    "sha256": digest,
                },
            },
            {"id": "Ethan", "name": "Ethan", "language": "en-US"},
        ],
        default_voice="Chelsie",
    )

    if tampered:
        contents = bytearray(clip.read_bytes())
        contents[-2] ^= 1
        clip.write_bytes(contents)
        with pytest.raises(ValueError, match="SHA-256"):
            voice_references(entry, tmp_path)
        return

    references = voice_references(entry, tmp_path)

    assert set(references) == {("Chelsie", entry.voices[0].reference.sha256)}
    assert references["Chelsie", entry.voices[0].reference.sha256].text == "Hi."
