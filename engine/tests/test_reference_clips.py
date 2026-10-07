"""The pure parts of the Voice Reference clip pipeline, without a network."""

import numpy as np
import pytest
from reference_clips import (
    SAMPLE_RATE,
    TOKEN_SAMPLES,
    Clip,
    clean_transcript,
    cloning_entry_dirs,
    finish_tail,
    join_with_gaps,
    normalise_peak,
    score,
    take_members,
    trim_to_speech,
)


@pytest.mark.parametrize(
    ("raw", "read_aloud"),
    [
        (
            "There is , according to legend, a boiling pot of gold at one end.",
            "There is, according to legend, a boiling pot of gold at one end.",
        ),
        (
            "People look, but no one ever finds it. \n",
            "People look, but no one ever finds it.",
        ),
        (
            "one of the most promising of these means ?",
            "one of the most promising of these means?",
        ),
        (
            "and to discuss plans as well as his,",
            "and to discuss plans as well as his.",
        ),
        ("making a fool of it;", "making a fool of it."),
    ],
)
def test_a_corpus_transcript_is_tidied_into_a_sentence(raw: str, read_aloud: str):
    assert clean_transcript(raw) == read_aloud


@pytest.mark.parametrize(
    "raw",
    [
        "She counted 3 red bags.",
        "She can scoop (these things) into bags.",
        'He said "no" to the station.',
        "Ask her: Wednesday at the train station.",
        "Bags [three] of them.",
    ],
)
def test_a_transcript_a_reader_would_stumble_on_is_refused(raw: str):
    with pytest.raises(ValueError, match="edit"):
        clean_transcript(raw)


def test_a_take_names_the_zip_members_of_consecutive_utterances():
    assert take_members("p299", [9, 10], "mic1") == [
        ("txt/p299/p299_009.txt", "wav48_silence_trimmed/p299/p299_009_mic1.flac"),
        ("txt/p299/p299_010.txt", "wav48_silence_trimmed/p299/p299_010_mic1.flac"),
    ]


@pytest.mark.parametrize("utterances", [[], [9, 11], [10, 9], [1, 2, 3, 4, 5]])
def test_a_take_is_one_to_four_consecutive_utterances(utterances: list[int]):
    with pytest.raises(ValueError, match="consecutive"):
        take_members("p299", utterances, "mic1")


def tone(seconds: float, level: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_trimming_keeps_the_speech_and_a_short_pad_of_room_either_side():
    silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
    padded = np.concatenate([silence, tone(1.0), silence])
    trimmed = trim_to_speech(padded, SAMPLE_RATE)
    assert abs(len(trimmed) - int(1.16 * SAMPLE_RATE)) <= 2


def test_trimming_refuses_a_silent_take():
    with pytest.raises(ValueError, match="silent"):
        trim_to_speech(np.zeros(SAMPLE_RATE, dtype=np.float32), SAMPLE_RATE)


def test_joined_utterances_are_separated_by_the_gap():
    joined = join_with_gaps([tone(1.0), tone(2.0)], SAMPLE_RATE, gap_s=0.45)
    assert len(joined) == int(3.45 * SAMPLE_RATE)
    gap = joined[SAMPLE_RATE : SAMPLE_RATE + int(0.45 * SAMPLE_RATE)]
    assert not gap.any()


def test_normalising_lands_the_loudest_sample_on_the_peak():
    assert normalise_peak(tone(1.0, level=0.2), peak=0.7).max() == pytest.approx(
        0.7, abs=1e-3
    )


def test_a_finished_clip_ends_in_whole_tokens_of_silence():
    finished = finish_tail(tone(1.0), SAMPLE_RATE)
    assert TOKEN_SAMPLES == 1920
    assert len(finished) % TOKEN_SAMPLES == 0
    assert len(finished) >= SAMPLE_RATE + int(0.36 * SAMPLE_RATE)
    assert not finished[-int(0.36 * SAMPLE_RATE) :].any()
    assert abs(finished[SAMPLE_RATE - 1]) < 1e-3
    assert np.abs(finished[: SAMPLE_RATE // 2]).max() == pytest.approx(0.5, abs=1e-3)


def test_the_score_reads_duration_noise_floor_and_headroom_off_the_clip():
    rng = np.random.default_rng(0)
    quiet = (rng.standard_normal(SAMPLE_RATE) * 0.001).astype(np.float32)
    clip = np.concatenate([quiet, tone(8.0, level=0.7), quiet])
    result = score(clip, SAMPLE_RATE)
    assert result.duration_s == pytest.approx(10.0)
    assert -65 < result.noise_floor_db < -55
    assert result.snr_db > 50
    assert result.fit == "ok"
    assert score(tone(3.0), SAMPLE_RATE).fit == "short"
    assert score(tone(11.0), SAMPLE_RATE).fit == "long"


def test_every_cloning_entry_gets_the_clip_and_no_preset_model_does():
    dirs = cloning_entry_dirs()
    assert [str(d.relative_to(d.parents[2])) for d in dirs] == [
        "references/qwen3-tts/0.6b",
        "references/qwen3-tts/1.7b",
        "references/voxcpm2/2b",
    ]


def test_a_stanza_names_the_clip_where_its_entry_keeps_it():
    clip = Clip(
        "Avery", "en-US", np.zeros(SAMPLE_RATE), "Words.", {"license": "CC-BY-4.0"}
    )
    target = cloning_entry_dirs()[0]

    assert clip.stanza(target)["reference"] == {
        "clip": "qwen3-tts/0.6b/Avery.wav",
        "text": "Words.",
        "attribution": {"license": "CC-BY-4.0"},
    }
