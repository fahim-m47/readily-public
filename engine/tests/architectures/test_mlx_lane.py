"""The MLX lane gates finished Blocks and scrubs in-pause crackle.

Everything here drives the loader through an injected fake model so the
suite runs on ubuntu; the real MLX path belongs to the macOS smoke lane.
"""

import hashlib
from types import SimpleNamespace

# This is the one real-client regression: its cache is empty and sockets are
# replaced with a failure, so it proves model loading cannot become egress.
import huggingface_hub  # nosemgrep: engine-no-egress-outside-download
import numpy as np
import pytest
from conftest import make_entry
from fakes import (
    SAMPLE_RATE,
    FakeModel,
    clean_chunk,
    crackle,
    degenerate_chunk,
    mlx_model_dir,
    synthesizer,
    tone,
    write_wav,
)
from generation_fakes import record
from huggingface_hub import constants  # nosemgrep: engine-no-egress-outside-download
from huggingface_hub.errors import (  # nosemgrep: engine-no-egress-outside-download
    LocalEntryNotFoundError,
)

from readily_engine.loading import qwen3
from readily_engine.loading.mlx_lane import MlxSynthesizer, runaway_budget_seconds
from readily_engine.loading.references import voice_references

model_dir = pytest.fixture(mlx_model_dir)


def test_model_loading_cannot_reach_hugging_face_and_restores_online_mode(
    model_dir, monkeypatch, tmp_path
):
    model = FakeModel([[clean_chunk()]])
    was_offline = constants.HF_HUB_OFFLINE

    def socket_would_be_egress(*_args, **_kwargs):
        raise AssertionError("model loading opened a network socket")

    monkeypatch.setattr("socket.socket", socket_would_be_egress)

    def load(_path):
        with pytest.raises(LocalEntryNotFoundError):
            huggingface_hub.hf_hub_download(
                repo_id="readily/no-egress-regression",
                filename="never-cached",
                cache_dir=tmp_path / "empty-hf-cache",
            )
        return model

    MlxSynthesizer(
        model_dir,
        seed_rng=lambda _seed: None,
        generate=qwen3.generate,
        runaway_seconds=qwen3.runaway_seconds,
        model_factory=load,
    )

    assert constants.is_offline_mode() is was_offline


def test_model_loading_restores_hugging_face_mode_after_a_load_failure(model_dir):
    was_offline = constants.HF_HUB_OFFLINE

    def fail(_path):
        assert constants.is_offline_mode() is True
        raise RuntimeError("load failed")

    with pytest.raises(RuntimeError, match="load failed"):
        MlxSynthesizer(
            model_dir,
            seed_rng=lambda _seed: None,
            generate=qwen3.generate,
            runaway_seconds=qwen3.runaway_seconds,
            model_factory=fail,
        )

    assert constants.is_offline_mode() is was_offline


def test_a_streamed_block_is_asked_for_by_voice_and_comes_back_as_one_run(model_dir):
    model = FakeModel([[clean_chunk(), clean_chunk()]])
    synth = synthesizer(model_dir, model)

    audio = synth.generate(record("Hello there.", "Chelsie")).pcm

    assert model.calls == [{"text": "Hello there.", "voice": "Chelsie", "speed": 1.0}]
    assert len(audio) == 2 * len(clean_chunk())


def sibilant_chunk(seconds: float = 2.0) -> np.ndarray:
    # Clean speech with heavy sibilance: voiced tone alternating with hf
    # hiss. Whole-chunk hf8k tops the static threshold, but the voiced
    # body says speech — the gate must not resynthesize it.
    rng = np.random.default_rng(21)
    pieces = []
    for index in range(int(seconds / 0.1)):
        if index % 2:
            pieces.append(tone(400, 0.1))
        else:
            hiss = 0.2 * rng.standard_normal(int(SAMPLE_RATE * 0.1))
            pieces.append(hiss.astype(np.float32))
    return np.concatenate(pieces)


def test_a_sibilant_first_chunk_is_not_mistaken_for_static(model_dir):
    # Measured on the real model: clean s-heavy openings score up to 0.41
    # hf8k on a 2s chunk. Only hf-heavy AND voiceless is degenerate.
    model = FakeModel([[sibilant_chunk(), clean_chunk()]])
    synth = synthesizer(model_dir, model)

    audio = synth.generate(record("Sixty-six sizzling sausages.", "Chelsie")).pcm

    assert len(model.calls) == 1
    assert len(audio) == len(sibilant_chunk()) + len(clean_chunk())


def test_a_degenerate_draw_fails_before_any_audio_flows(model_dir):
    model = FakeModel([[degenerate_chunk()], [clean_chunk()]])
    synth = synthesizer(model_dir, model)

    with pytest.raises(RuntimeError, match="degenerate"):
        synth.generate(record("Short input.", "Chelsie"))
    assert len(model.calls) == 1


def test_audio_after_the_gate_window_is_not_gated(model_dir):
    # Only the first two seconds are the calibrated accept window.
    model = FakeModel([[clean_chunk(), degenerate_chunk()]])
    synth = synthesizer(model_dir, model)

    audio = synth.generate(record("Longer input.", "Chelsie")).pcm

    assert len(model.calls) == 1
    assert len(audio) == 2 * len(clean_chunk())


def test_an_in_pause_crackle_burst_is_scrubbed(model_dir):
    quiet = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
    chunk = np.concatenate([clean_chunk(0.5), quiet, crackle(), quiet])
    model = FakeModel([[chunk, clean_chunk(0.5)]])
    synth = synthesizer(model_dir, model)

    audio = synth.generate(record("A pause, then speech.", "Chelsie")).pcm

    assert len(audio) == len(chunk) + len(clean_chunk(0.5))
    burst_start = len(clean_chunk(0.5)) + len(quiet)
    burst = audio[burst_start : burst_start + len(crackle())]
    assert float(np.max(np.abs(burst))) < 0.01
    # Speech on both sides is untouched.
    np.testing.assert_array_equal(audio[: SAMPLE_RATE // 4], chunk[: SAMPLE_RATE // 4])


def test_a_burst_straddling_a_chunk_seam_is_still_scrubbed(model_dir):
    quiet = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
    burst = crackle(0.06)
    split = len(burst) // 2
    first = np.concatenate([clean_chunk(1.0), quiet, burst[:split]])
    second = np.concatenate([burst[split:], quiet, clean_chunk(0.5)])
    model = FakeModel([[first, second]])
    synth = synthesizer(model_dir, model)

    audio = synth.generate(record("Burst on the seam.", "Chelsie")).pcm

    assert len(audio) == len(first) + len(second)
    burst_start = len(clean_chunk(1.0)) + len(quiet)
    scrubbed = audio[burst_start : burst_start + len(burst)]
    assert float(np.max(np.abs(scrubbed))) < 0.01


def quiet_chunk(seconds: float = 2.0) -> np.ndarray:
    # Under the burst detector's 0.008 active-frame RMS, but not digital
    # zero — a runaway's tail carries stray low-level junk, not silence.
    rng = np.random.default_rng(8)
    return (0.001 * rng.standard_normal(int(SAMPLE_RATE * seconds))).astype(np.float32)


def test_a_runaway_of_near_silence_ends_the_utterance(model_dir):
    # The measured EOS-miss mode: real speech, then the model pads toward
    # its maximum length with near-silence. The stream must end within the
    # silence cutoff instead of playing minutes of nothing.
    model = FakeModel([[clean_chunk(1.0)] + [quiet_chunk(2.0)] * 10])
    result = synthesizer(model_dir, model).generate(record("Hi.", "Chelsie"))
    assert result.cutoffs == 1
    audio = result.pcm
    assert len(model.calls) == 1
    assert len(audio) / SAMPLE_RATE <= 3.1  # speech + cutoff, not 21s


def test_a_runaway_of_babble_is_cut_at_the_text_length_budget(model_dir):
    # A runaway that keeps emitting audible junk never trips the silence
    # cutoff; the text-proportional budget is the backstop.
    model = FakeModel([[clean_chunk(2.0)] * 10])
    result = synthesizer(model_dir, model).generate(record("Hi.", "Chelsie"))
    assert result.cutoffs == 1
    audio = result.pcm
    assert len(model.calls) == 1
    assert len(audio) / SAMPLE_RATE <= 8.0  # well under the scripted 20s


def test_the_runaway_budget_is_whatever_the_lane_was_built_with(model_dir):
    model = FakeModel([[clean_chunk(2.0)] * 10])
    lane = synthesizer(model_dir, model, runaway_seconds=lambda _text: 3.0)
    result = lane.generate(record("Hi.", "Chelsie"))
    assert result.cutoffs == 1
    assert len(result.pcm) / SAMPLE_RATE == 4.0


def test_a_natural_pause_does_not_trip_the_runaway_cutoff(model_dir):
    # In-speech pauses top out around the entry's 400ms paragraph break;
    # even a generous one must stream through untouched.
    scripted = [clean_chunk(2.0), quiet_chunk(1.0), clean_chunk(2.0)]
    model = FakeModel([scripted])
    result = synthesizer(model_dir, model).generate(record("Hi.", "Chelsie"))
    assert result.cutoffs == 0
    audio = result.pcm
    assert len(audio) == sum(len(chunk) for chunk in scripted)


def test_the_lane_synthesizes_the_block_it_was_given_whole(model_dir):
    model = FakeModel([[clean_chunk()], [clean_chunk()]])
    synth = synthesizer(model_dir, model)
    text = "A first sentence to start. " + "More words follow here. " * 4

    synth.generate(record(text, "Chelsie"))

    assert model.calls == [{"text": text, "voice": "Chelsie", "speed": 1.0}]


def test_the_gate_reads_the_finished_head_across_stream_chunks(model_dir):
    model = FakeModel([[degenerate_chunk(0.1), clean_chunk(1.9)]])

    audio = synthesizer(model_dir, model).generate(record("Hello.", "Chelsie")).pcm

    assert len(model.calls) == 1
    assert len(audio) == 2 * SAMPLE_RATE


def test_non_streaming_gates_the_head_without_diluting_it_with_later_speech(model_dir):
    model = FakeModel([[degenerate_chunk(), clean_chunk(8)], [clean_chunk(1)]])
    lane = synthesizer(model_dir, model, qwen3.generate)

    with pytest.raises(RuntimeError, match="degenerate"):
        lane.generate(
            record(
                "A longer paragraph with plenty of clean speech.",
                "Chelsie",
                decode_mode="non-streaming",
            )
        )

    assert len(model.calls) == 1


def test_non_streaming_keeps_real_speech_in_the_first_120ms(model_dir):
    model = FakeModel([[clean_chunk(1)]])
    lane = synthesizer(model_dir, model, qwen3.generate)

    audio = lane.generate(record("Hello.", "Chelsie", decode_mode="non-streaming")).pcm

    np.testing.assert_array_equal(audio, clean_chunk(1))


@pytest.mark.parametrize("recovers", [False, True])
def test_worker_retries_a_rejected_mlx_block_once_in_total(
    tmp_path, model_dir, recovers
):
    from conftest import wait_until
    from storage_fakes import open_test_storage
    from worker_fakes import RecordingPlayback, request, worker_for

    seeds = []

    def generate(_model, generation, reference):
        recovered = recovers and seeds[-1] != seeds[0]
        pcm = clean_chunk() if recovered else degenerate_chunk()
        yield SimpleNamespace(audio=pcm, sample_rate=SAMPLE_RATE)

    lane = MlxSynthesizer(
        model_dir,
        generate=generate,
        runaway_seconds=runaway_budget_seconds,
        seed_rng=seeds.append,
        model_factory=lambda _path: object(),
    )
    storage = open_test_storage(tmp_path)
    playback = RecordingPlayback()
    worker = worker_for(lane, playback, storage)
    try:
        narration_id = worker.start(request("Short input."))
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert len(seeds) == 2
        assert seeds[1] == (seeds[0] + 1) % 2**32
        assert storage.history_detail(narration_id).has_gaps is not recovers
        assert bool(playback.played) is recovers
        if recovers:
            plan = storage.plan(narration_id)
            segment = plan.segments[0]
            original = storage.raw_audio(segment).pcm.copy()
            assert seeds[1] == segment.generation.rng_seed
            storage.update_retention(segment_budget_bytes=1, keep_audio_days=None)
            assert storage.raw_audio(segment) is None
            worker.resume(narration_id)
            wait_until(lambda: worker.snapshot()["phase"] == "finished")
            assert seeds[2:] == seeds[1:2]
            assert (
                storage.plan(narration_id).segments[0].generation == segment.generation
            )
            np.testing.assert_array_equal(storage.raw_audio(segment).pcm, original)
    finally:
        worker.close()


def test_each_draw_is_seeded_from_the_record_and_receives_declared_parameters(
    model_dir,
):
    calls = []
    seeds = []

    def generate(_model, generation, reference):
        calls.append((generation, reference, seeds[-1]))
        pcm = degenerate_chunk() if len(calls) == 1 else clean_chunk()
        yield SimpleNamespace(audio=pcm, sample_rate=SAMPLE_RATE)

    lane = MlxSynthesizer(
        model_dir,
        generate=generate,
        runaway_seconds=runaway_budget_seconds,
        seed_rng=seeds.append,
        model_factory=lambda _path: object(),
    )
    generation = record(parameters={"temperature": 0.7}, seed=2**32 - 1)
    with pytest.raises(RuntimeError, match="degenerate"):
        lane.generate(generation)
    lane.generate(record(text="Unrelated."))
    lane.generate(generation)

    assert seeds == [2**32 - 1, record(text="Unrelated.").rng_seed, 2**32 - 1]
    assert calls[0] == (generation, None, 2**32 - 1)
    assert calls[2] == (generation, None, 2**32 - 1)


def test_voices_sharing_reference_bytes_keep_their_own_transcripts(model_dir):
    from readily_engine.generation import GenerationRecord

    clip = model_dir / "qwen3-tts/0.6b/Chelsie.wav"
    write_wav(clip, tone(200, 0.1))
    digest = hashlib.sha256(clip.read_bytes()).hexdigest()
    entry = make_entry(
        name="qwen3-tts",
        tag="0.6b",
        architecture="qwen3",
        default_voice="Chelsie",
        voices=[
            {
                "id": voice,
                "name": voice,
                "language": "en-US",
                "reference": {
                    "clip": "qwen3-tts/0.6b/Chelsie.wav",
                    "sha256": digest,
                    "text": transcript,
                    "attribution": None,
                },
            }
            for voice, transcript in [("Chelsie", "First."), ("Ethan", "Second.")]
        ],
    )
    calls = []

    def generate(_model, generation, reference):
        calls.append((generation.voice_id, reference.text))
        yield SimpleNamespace(audio=clean_chunk(), sample_rate=SAMPLE_RATE)

    lane = synthesizer(
        model_dir,
        object(),
        generate,
        references=voice_references(entry, model_dir),
    )
    for voice in entry.voices:
        lane.generate(GenerationRecord.for_entry(entry, voice.id, "Hello."))
    assert calls == [("Chelsie", "First."), ("Ethan", "Second.")]
