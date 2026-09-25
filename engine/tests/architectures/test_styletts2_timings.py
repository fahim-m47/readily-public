"""Word coordinates follow the ids actually sent to a StyleTTS2 graph."""

from dataclasses import dataclass

from readily_engine.loading.styletts2 import duration_timings


@dataclass
class Token:
    text: str
    phonemes: str
    whitespace: str = ""


def test_expanded_number_and_dropped_phone_keep_word_onsets():
    timings = duration_timings(
        "We 2026 café",
        [Token("We", "w?", " "), Token("2026", "aa aa", " "), Token("café", "c")],
        [1, 2, 3, 3, 2, 3, 3, 2, 4],
        [2, 3, 1, 2, 2, 1, 2, 2, 1, 4, 2],
        vocab={"w": 1, " ": 2, "a": 3, "c": 4},
        sample_count=13200,
    )
    assert [(t.start_char, t.end_char) for t in timings] == [(0, 2), (3, 7), (8, 12)]
    assert [(t.start_sec, t.end_sec) for t in timings] == [
        (0.05, 0.125),
        (0.15, 0.375),
        (0.4, 0.5),
    ]
    assert {t.provenance for t in timings} == {"spoken"}


def test_kitten_respelling_consumes_the_expanded_ids():
    from readily_engine.loading.styletts2.kitten import VOCAB, as_espeak

    timings = duration_timings(
        "go go",
        [Token("go", "O", " "), Token("go", "O")],
        [VOCAB[p] for p in "oʊ oʊ"],
        [2, 3, 1, 2, 4, 2, 1],
        vocab=VOCAB,
        sample_count=9000,
        respell=as_espeak,
    )
    assert [(t.start_char, t.start_sec, t.end_sec) for t in timings] == [
        (0, 0.05, 0.15),
        (3, 0.2, 0.35),
    ]


def test_any_mapping_mismatch_rejects_the_whole_block():
    cases = [
        ([Token("a", "a", " "), Token("b", "b")], [1, 2, 1], [1] * 5, 3000),
        ([Token("a", "a", " "), Token("b", "b")], [1, 2, 3], [1] * 4, 2400),
        ([Token("a", "a", " "), Token("b", "b")], [1, 2, 3], [1] * 5, 2999),
        ([Token("a", "a", " "), Token("x", "b")], [1, 2, 3], [1] * 5, 3000),
        ([Token("a", "a", " "), Token("b", "b")], [1, 2, 3], [1, 1, 0, 2, 1], 3000),
        ([Token("a b", "a b")], [1, 2, 3], [1] * 5, 3000),
    ]
    for tokens, ids, durations, samples in cases:
        assert (
            duration_timings(
                "a b",
                tokens,
                ids,
                durations,
                vocab={"a": 1, " ": 2, "b": 3},
                sample_count=samples,
            )
            == ()
        )
