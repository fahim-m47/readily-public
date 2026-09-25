"""Known text and PCM enter alignment; bounded word timings leave it."""

import numpy as np
import pytest

from readily_engine.alignment import CTCAligner


def test_repeated_letters_and_words_keep_their_original_text_offsets():
    labels = ("<pad>", "|", "A", "L")
    frames = [0, 2, 3, 0, 3, 1, 0, 2, 0]
    probabilities = np.full((len(frames), len(labels)), 0.001, dtype=np.float32)
    probabilities[np.arange(len(frames)), frames] = 0.997
    seen = []

    def infer(pcm):
        seen.append(len(pcm))
        return np.log(probabilities)

    aligner = CTCAligner(infer, labels)
    words = aligner.align("All, A!", np.ones(4320, dtype=np.float32), 24000)

    assert seen == [2880]
    assert [(word.start_char, word.end_char) for word in words] == [(0, 3), (5, 6)]
    assert [word.start_sec for word in words] == pytest.approx([0.02, 0.14])
    assert all(word.provenance == "matched" for word in words)


def test_uncertain_words_degrade_without_losing_the_good_word():
    probabilities = np.array(
        [[0.01, 0.01, 0.97, 0.01], [0.01, 0.97, 0.01, 0.01], [0.3, 0.1, 0.3, 0.3]],
        dtype=np.float32,
    )
    aligner = CTCAligner(lambda pcm: np.log(probabilities), ("<pad>", "|", "A", "B"))
    words = aligner.align("A B", np.ones(1600, dtype=np.float32), 16000)
    assert [(word.start_char, word.end_char) for word in words] == [(0, 1)]


@pytest.mark.parametrize("text", ["A 42", "A café", "!!!", "AAA"])
def test_unrepresentable_or_impossible_transcripts_have_no_guessed_timings(text):
    probabilities = np.array([[0.01, 0.01, 0.98]], dtype=np.float32)
    aligner = CTCAligner(lambda pcm: np.log(probabilities), ("<pad>", "|", "A"))
    assert aligner.align(text, np.ones(1600, dtype=np.float32), 16000) == ()


def test_unexplained_speech_cannot_leave_a_confident_word_at_the_wrong_repeat():
    probabilities = np.full((10, 4), 0.001, dtype=np.float32)
    probabilities[:, 2] = 1e-8
    probabilities[:, 3] = 0.997
    probabilities[0] = [0.001, 0.001, 0.997, 0.001]
    aligner = CTCAligner(lambda pcm: np.log(probabilities), ("<pad>", "|", "A", "B"))
    assert aligner.align("A", np.ones(3200, dtype=np.float32), 16000) == ()
