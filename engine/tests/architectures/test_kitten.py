"""Kitten TTS Nano's Architecture: what is unique to it is respelling
Misaki's IPA into the symbols this export was trained on, and repairing the
DC drift and overshoot it ships in every draw. Everything every Architecture
owes is `TestConformance`'s."""

# IPA test fixtures are intentionally confusable with Latin letters, and this
# module's comments have to spell phonemes out to say what the ids mean.
# ruff: noqa: RUF001, RUF003
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from conformance import Conformance
from fakes import FakeOnnxSession
from generation_fakes import record

from readily_engine.catalog import CatalogEntry
from readily_engine.loading.styletts2 import SAMPLE_RATE, StyleTTS2Synthesizer
from readily_engine.loading.styletts2.kitten import (
    MODEL_FILE,
    SPEC,
    VOCAB,
    VOICES_FILE,
    as_espeak,
)

VOICES = ("expr-voice-2-f", "expr-voice-5-m")


def promote(model_dir: Path, voices: Sequence[str] = VOICES) -> Path:
    """Write a graph placeholder and one style vector per Voice."""
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / MODEL_FILE).write_bytes(b"placeholder")
    with (model_dir / VOICES_FILE).open("wb") as output:
        np.savez(output, **{voice: np.ones((1, 256), np.float32) for voice in voices})
    return model_dir


class TestConformance(Conformance):
    model_id = "kitten-tts:15m"
    sample_rate = SAMPLE_RATE
    runaway_budget = False

    def promote(self, model_dir: Path, entry: CatalogEntry) -> None:
        promote(model_dir, [voice.id for voice in entry.voices])


def speaking(audio: np.ndarray | None = None) -> FakeOnnxSession:
    """A session whose every draw is `audio`."""
    if audio is None:
        audio = np.array([0.1, 0.2, -0.1], dtype=np.float32)
    return FakeOnnxSession(lambda _feed: [audio])


def synthesizer(root: Path, session, phonemes: str = "həlˈO") -> StyleTTS2Synthesizer:
    return StyleTTS2Synthesizer(
        root,
        SPEC,
        session_factory=lambda path: session,
        g2p_factory=lambda _dialect: lambda text: (phonemes, [text]),
    )


def test_the_style_vector_is_the_voices_own_and_does_not_vary_with_length(tmp_path):
    # Kokoro indexes its style rows by phoneme count; this export carries one
    # vector per Voice, so indexing it the Kokoro way would feed the graph a
    # single float row and silently change the Voice.
    root = promote(tmp_path)
    short = speaking()
    long = speaking()
    synthesizer(root, short, "hˈO").generate(record("Hi", "expr-voice-5-m"))
    synthesizer(root, long, "həlˈOʊ ðˈɛɹ").generate(
        record("Hello there", "expr-voice-5-m")
    )

    assert short.inputs["style"].shape == (1, 256)
    assert short.inputs["style"].dtype == np.float32
    np.testing.assert_array_equal(short.inputs["style"], long.inputs["style"])


def test_text_that_misaki_cannot_phonemize_is_not_sent_to_onnx(tmp_path):
    session = speaking()
    with pytest.raises(ValueError, match="no supported phonemes"):
        synthesizer(promote(tmp_path), session, "").generate(
            record("unknown", "expr-voice-2-f")
        )
    assert not session.calls


def draw(pcm: np.ndarray, tmp_path: Path) -> np.ndarray:
    """What the loader returns when the graph produces exactly `pcm`."""
    return (
        synthesizer(promote(tmp_path), speaking(pcm))
        .generate(record("Hello", "expr-voice-2-f"))
        .pcm
    )


def test_the_bias_under_the_speech_goes_without_taking_the_silence_with_it(tmp_path):
    # This export voices on a positive offset and pauses at true zero. One
    # mean over the whole draw leaves the speech tilted and pushes the silence
    # down to minus that mean, where `trim_silence` and the crackle scrubber
    # both read it as sound — the pause stops being a pause.
    rate = SAMPLE_RATE
    time = np.arange(rate, dtype=np.float32) / rate
    speech = 0.4 * np.sin(2 * np.pi * 200 * time) + 0.05
    biased = np.concatenate([np.zeros(rate, dtype=np.float32), speech])

    cleaned = draw(biased.astype(np.float32), tmp_path)

    quiet = cleaned[: rate // 2]
    voiced = cleaned[rate + rate // 2 :]
    assert np.max(np.abs(quiet)) < 1e-4
    assert abs(float(np.mean(voiced))) < 1e-3


def test_a_draw_that_overshoots_full_scale_comes_back_inside_it(tmp_path):
    rate = SAMPLE_RATE
    time = np.arange(rate, dtype=np.float32) / rate
    loud = (1.17 * np.sin(2 * np.pi * 200 * time)).astype(np.float32)

    cleaned = draw(loud, tmp_path)

    assert float(np.max(np.abs(cleaned))) < 0.999


def test_the_symbol_table_agrees_with_upstreams_own_ordering():
    # The graph's ids are its contract and nothing at runtime can check them:
    # a wrong id is a valid id, so the export narrates a different sound and
    # no error is raised. The pinned revision ships no tokenizer file to
    # compare against either, so the comparison is against the construction
    # upstream's `TextCleaner` performs — the pad symbol, then four literal
    # strings, numbered by position. Transcribing the result by hand is what
    # put `"` on the id for `”` and left 11 and 14 unreachable.
    pad = "$"
    punctuation = ';:,.!?¡¿—…"«»“” '
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    letters_ipa = (
        "ɑɐɒæɓʙβɔɕçɗɖðʤəɘɚɛɜɝɞɟʄɡɠɢʛɦɧħɥʜɨɪʝɭɬɫɮʟɱɯɰŋɳɲɴøɵɸθœɶʘɹɺɾɻʀʁɽʂʃʈʧʉʊʋⱱʌɣɤʍχʎʏʑʐʒ"
        "ʔʡʕʢǀǁǂǃˈˌːˑʼʴʰʱʲʷˠˤ˞↓↑→↗↘'̩'ᵻ"
    )
    # A later duplicate wins, exactly as it would in a dict literal — the
    # apostrophe appears twice in the IPA string, which is why 174 is absent
    # from `VOCAB` rather than missing from it.
    upstream = {
        symbol: index
        for index, symbol in enumerate(pad + punctuation + letters + letters_ipa)
    }
    del upstream[pad]

    assert upstream == VOCAB


def test_misakis_diphthong_shorthand_is_spelled_out_before_the_graph_sees_it(tmp_path):
    # Misaki writes American English in Kokoro's phoneme set, where `A` is the
    # whole of /eɪ/. This export reads espeak-ng IPA, and `A` is in its table
    # anyway — id 17, the Latin capital — so the shorthand sails past the
    # `token_ids` guard and narrates a vowel the export barely saw in
    # training. "paste" came out as "pust" that way.
    session = speaking()
    synthesizer(promote(tmp_path), session, "pˈAst").generate(
        record("paste", "expr-voice-2-f")
    )

    # p ˈ e ɪ s t — not p ˈ A s t.
    assert session.inputs["input_ids"].tolist() == [[0, 58, 156, 47, 102, 61, 62, 0]]


def test_the_affricates_go_in_as_the_two_symbols_espeak_writes_them_with(tmp_path):
    # Misaki runs espeak with `tie='^'` and folds `d^ʒ`/`t^ʃ` into ligatures;
    # this export never saw the folded form. `ʤ` is id 82 and a perfectly
    # valid id, so nothing downstream objects — every j/ch/dge/-ture sound in
    # the language was simply voiced off the wrong one.
    session = speaking()
    synthesizer(promote(tmp_path), session, "ʤˈʌʤ").generate(
        record("judge", "expr-voice-2-f")
    )

    # d ʒ ˈ ʌ d ʒ — not ʤ ˈ ʌ ʤ.
    assert session.inputs["input_ids"].tolist() == [
        [
            0,
            VOCAB["d"],
            VOCAB["ʒ"],
            156,
            VOCAB["ʌ"],
            VOCAB["d"],
            VOCAB["ʒ"],
            0,
        ]
    ]


def test_misakis_synthetic_flap_goes_in_as_the_ipa_flap(tmp_path):
    session = speaking()
    # The default G2P is intentional: this covers installed Misaki output.
    instance = StyleTTS2Synthesizer(
        promote(tmp_path),
        SPEC,
        session_factory=lambda path: session,
    )

    instance.generate(record("water", "expr-voice-2-f"))

    row = session.inputs["input_ids"].tolist()[0]
    assert row == [
        0,
        VOCAB["w"],
        VOCAB["ˈ"],
        VOCAB["ɔ"],
        VOCAB["ɾ"],
        VOCAB["ə"],
        VOCAB["ɹ"],
        0,
    ]
    assert VOCAB["T"] not in row


def test_the_symbols_misaki_composes_are_spelled_back_out():
    # The one check that can actually fail on this bug. `VOCAB` membership
    # cannot: `A` is id 17 and `ʤ` is id 82, so a missing entry leaves every
    # id valid and every downstream guard quiet — which is exactly how the
    # first version of this shipped. So pin the mapping itself, symbol by
    # symbol, against Misaki's own alphabet. Synthetic `T` is not in `US_VOCAB`,
    # so the integration test above covers it through default Misaki output.
    # Deleting an entry shrinks this dict; mistyping one changes a value; both
    # fail here and nowhere else.
    from misaki.en import US_VOCAB

    composed = {
        symbol: as_espeak(symbol)
        for symbol in sorted(US_VOCAB)
        if as_espeak(symbol) != symbol
    }

    assert composed == {
        "A": "eɪ",
        "I": "aɪ",
        "O": "oʊ",
        "W": "aʊ",
        "Y": "ɔɪ",
        "ʤ": "dʒ",
        "ʧ": "tʃ",
        "ᵊ": "ə",
    }


def test_no_symbol_misaki_can_emit_reaches_the_graph_without_an_id():
    # The weaker, still-worth-having half: a symbol with no id at all is
    # dropped by `token_ids` in silence. That is what `ᵊ` was doing — a lost
    # sound rather than a wrong one. This catches Misaki growing its alphabet;
    # it cannot catch a symbol whose id is real but wrong, which is why the
    # test above exists.
    from misaki.en import US_VOCAB

    unspellable = sorted(
        symbol
        for symbol in US_VOCAB
        if any(phone not in VOCAB for phone in as_espeak(symbol))
    )

    assert unspellable == []


def test_the_row_is_bracketed_by_the_pads_and_nothing_else(tmp_path):
    # Upstream's pinned wrapper writes `tokens.insert(0, 0); tokens.append(0)`
    # and nothing else (`kittentts.onnx_model`, the 0.1.0 wrapper the pinned
    # model card points to). An earlier revision here appended id 10 before
    # the closing pad and documented that as upstream's bracketing; id 10 is
    # `…`'s, so every utterance was narrated with a synthetic ellipsis that
    # lengthened the draw. The row carries the phoneme ids and the pads, and
    # no id the phonemes did not put there.
    session = speaking()
    synthesizer(promote(tmp_path), session, "h\u02c8a\u026a").generate(
        record("Hi", "expr-voice-2-f")
    )

    row = session.inputs["input_ids"].tolist()[0]
    assert row == [0, VOCAB["h"], VOCAB["ˈ"], VOCAB["a"], VOCAB["ɪ"], 0]
    assert session.inputs["input_ids"].dtype == np.int64
