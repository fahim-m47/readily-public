"""Unknown words survive the same Misaki interface both instant voices use."""

# IPA fixtures deliberately resemble Latin letters.
# ruff: noqa: RUF001

import json
from types import SimpleNamespace

import numpy as np
import pytest

from readily_engine.loading.pronunciation import (
    PronunciationFallback,
    ipa_to_misaki,
)
from readily_engine.loading.styletts2 import misaki_g2p


class Graph:
    """Answers every word with the NRC padding-then-repeat logits for Zyntrix."""

    def __init__(self) -> None:
        self.calls: list[list[list[int]]] = []

    def run(self, outputs, inputs):
        self.calls.append(inputs["text"].tolist())
        ids = [2, 27, 27, 0, 27, 42, 16, 16, 21, 40, 42, 13, 20, 20, 3]
        return [np.eye(53, dtype=np.float32)[ids][None, :]]


# What `word_ids` produces for "zyntrix": three tokens per letter between the
# language and end tokens.
ZYNTRIX_IDS = [
    [2, *[29] * 3, *[28] * 3, *[17] * 3, *[23] * 3, *[21] * 3, *[12] * 3, *[27] * 3, 3]
]


def fallback_with(
    tmp_path, lexicon: dict[str, str]
) -> tuple[PronunciationFallback, Graph]:
    (tmp_path / "g2p.onnx").write_bytes(b"verified graph stand-in")
    (tmp_path / "lexicon.json").write_text(json.dumps(lexicon))
    graph = Graph()
    return PronunciationFallback(tmp_path, session_factory=lambda _: graph), graph


def test_lexicon_hit_never_runs_the_graph(tmp_path):
    fallback, graph = fallback_with(tmp_path, {"zyntrix": "zɪntɹɪks"})
    g2p = misaki_g2p(fallback=fallback)

    assert g2p("Zyntrix met Zyntrix.")[0] == "zˈɪntɹɪks mˈɛt zˈɪntɹɪks."
    assert fallback(SimpleNamespace(text="ZYNTRIX")) == ("zˈɪntɹɪks", 2)
    assert graph.calls == []


def test_unknown_word_is_decoded_and_cached_across_case(tmp_path):
    fallback, graph = fallback_with(tmp_path, {})
    assert fallback(SimpleNamespace(text="Zyntrix")) == ("zˈɪntɹɪks", 2)
    assert fallback(SimpleNamespace(text="ZYNTRIX")) == ("zˈɪntɹɪks", 2)
    assert graph.calls == [ZYNTRIX_IDS]


def test_accented_letters_fold_instead_of_vanishing(tmp_path):
    fallback, graph = fallback_with(tmp_path, {})
    assert fallback(SimpleNamespace(text="Zyntríx")) == ("zˈɪntɹɪks", 2)
    assert graph.calls == [ZYNTRIX_IDS]


def test_apostrophes_stay_inside_the_word(tmp_path):
    fallback, graph = fallback_with(tmp_path, {"o'brien": "oʊbɹaɪən"})
    assert fallback(SimpleNamespace(text="O'Brien")) == ("ˈObɹIən", 2)
    assert fallback(SimpleNamespace(text="Zyntrix's")) == ("zˈɪntɹɪks", 2)
    assert len(graph.calls) == 1


def test_compound_parts_are_pronounced_separately(tmp_path):
    fallback, graph = fallback_with(tmp_path, {"well": "wɛl"})
    assert fallback(SimpleNamespace(text="well-Zyntrix2")) == ("wˈɛl zˈɪntɹɪks", 2)
    assert len(graph.calls) == 1


@pytest.mark.parametrize("word", ["!!!", "123", "'", "😀"])
def test_symbols_do_not_infer(tmp_path, word):
    fallback, graph = fallback_with(tmp_path, {})
    assert fallback(SimpleNamespace(text=word)) == ("", 0)
    assert graph.calls == []


@pytest.mark.parametrize(
    ("ipa", "expected"),
    [
        ("oʊ eɪ aɪ aʊ ɔɪ t͡ʃ d͡ʒ", "O A I W Y ʧ ʤ"),
        ("ɝ g r ʔ ɐ x ç ɪə", "ɜɹ ɡ ɹ t ə k k iə"),
        ("ˈhəloʊ", "ˈhəlO"),
        ("y ø œ ʏ ʁ", "i ɜ ɛ ɪ ɹ"),
    ],
)
def test_ipa_mapping(ipa, expected):
    assert ipa_to_misaki(ipa) == expected


def test_every_checkpoint_symbol_maps_into_misakis_us_alphabet():
    from misaki.en import US_VOCAB

    # Copied independently from the pinned checkpoint's phoneme tokenizer.
    symbols = "abdefghijklmnoprstuvwxyzæçðøŋœɐɑɔəɛɝɹɡɪʁʃʊʌʏʒʔː͡θ"
    assert set(ipa_to_misaki(" ".join(symbols))) <= US_VOCAB | {" "}


def test_bundled_files_match_pins_and_include_both_notices():
    from readily_engine.loading.pronunciation import (
        BUNDLED_DIRECTORY,
        verify_bundled_files,
    )

    verify_bundled_files()
    notice = (BUNDLED_DIRECTORY / "LICENSE").read_text()
    assert "Copyright (c) 2021 Axel Springer" in notice
    assert "Permission is hereby granted" in notice
    assert "Carnegie Mellon University" in notice
    assert "Redistributions in binary form must reproduce" in notice


def test_corrupt_bundle_is_rejected_before_misaki_can_load_it(tmp_path, monkeypatch):
    from readily_engine.loading import pronunciation

    (tmp_path / "g2p.onnx").write_bytes(b"wrong graph")
    (tmp_path / "lexicon.json").write_text("{}")
    monkeypatch.setattr(pronunciation, "BUNDLED_DIRECTORY", tmp_path)
    pronunciation.bundled_fallback.cache_clear()
    try:
        with pytest.raises(ValueError, match="pronunciation asset hash mismatch"):
            misaki_g2p()
    finally:
        pronunciation.bundled_fallback.cache_clear()


def test_default_misaki_speaks_unknown_words_and_shares_the_fallback():
    first, second = misaki_g2p(), misaki_g2p()
    assert first.fallback is second.fallback
    assert first("Zyntrix met Zyntrix.")[0] == "zˈɪntɹɪks mˈɛt zˈɪntɹɪks."


def test_a_british_g2p_says_british_words():
    # The dialect argument has to survive into misaki, not just into the
    # Kokoro loader's cache key: a renamed upstream kwarg would leave every
    # British Voice speaking American with nothing else failing.
    text = "The car park water schedule."

    assert misaki_g2p("american")(text)[0] == "ðə kˈɑɹ pˈɑɹk wˈɔTəɹ skˈɛʤˌul."
    assert misaki_g2p("british")(text)[0] == "ðə kˈɑː pˈɑːk wˈɔːtə ʃˈɛdjuːl."
