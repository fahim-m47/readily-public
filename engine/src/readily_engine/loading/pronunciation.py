"""NRC's English lexicon and ONNX G2P, used only for Misaki's unknown words.

The app carries the hash-pinned ONNX graph, lexicon and MIT notice alongside
this module. Nothing is downloaded here.
"""

# IPA symbols deliberately resemble Latin letters.
# ruff: noqa: RUF001

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from functools import cache
from itertools import groupby
from pathlib import Path
from typing import Protocol

import numpy as np

from readily_engine.loading.onnx import Session, onnx_session

# Token order read from the pinned NRC checkpoint, not alphabetical order.
TEXT_SYMBOLS = (
    "_",
    "<de>",
    "<en_us>",
    "<end>",
    *"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZäöüÄÖÜß'",
)
PHONEME_SYMBOLS = (
    "_",
    "<de>",
    "<en_us>",
    "<end>",
    *"abdefghijklmnoprstuvwxyzæçðøŋœɐɑɔəɛɝɹɡɪʁʃʊʌʏʒʔː͡θ",
)
_TEXT_IDS = {symbol: index for index, symbol in enumerate(TEXT_SYMBOLS)}

BUNDLED_DIRECTORY = Path(__file__).parent / "data" / "pronunciation"
_BUNDLED_HASHES = {
    "g2p.onnx": "ffc92ce689b8890b2d001a87697ca909d6b7823ce4da2de96e490fc2b12ff105",
    "lexicon.json": "2a859ed905f819a7f7c346c93a15604524fe647612b6406ee967cc3d99518a6e",
}


def verify_bundled_files() -> None:
    """Refuse altered pronunciation data before parsing JSON or loading ONNX."""
    for name, expected in _BUNDLED_HASHES.items():
        with (BUNDLED_DIRECTORY / name).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"pronunciation asset hash mismatch: {name}")


def word_ids(word: str) -> list[int]:
    """NRC's English tokenizer: lower case, repeat characters three times.

    Accents fold to their base letter so "Beyoncé" is tokenised whole rather
    than truncated to a different word.
    """
    letters = unicodedata.normalize("NFKD", word.lower())
    return [
        2,
        *(_TEXT_IDS[c] for c in letters if c in _TEXT_IDS for _ in range(3)),
        3,
    ]


def decode_logits(logits: np.ndarray) -> str:
    """NRC's decode, including removal of padding *before* deduplication."""
    ids = logits[0].argmax(axis=-1)
    return "".join(
        PHONEME_SYMBOLS[index]
        for index, _ in groupby(index for index in ids if index != 0)
        if index >= 4
    )


def ipa_to_misaki(ipa: str) -> str:
    """Misaki's US E2M rules for the symbols the NRC checkpoint can emit.

    The non-English rounded vowels map to their nearest English vowels;
    both Voice Models receive symbols their US front end understands.
    """
    ps = ipa.replace("͡", "")
    for source, target in (
        ("aɪ", "I"),
        ("aʊ", "W"),
        ("dʒ", "ʤ"),
        ("eɪ", "A"),
        ("tʃ", "ʧ"),
        ("ɔɪ", "Y"),
        ("ɝ", "ɜɹ"),
        ("r", "ɹ"),
        ("x", "k"),
        ("ç", "k"),
        ("ɐ", "ə"),
        ("oʊ", "O"),
        ("ɪə", "iə"),
        ("ː", ""),
        ("o", "ɔ"),
        ("e", "A"),
        ("g", "ɡ"),
        ("ʔ", "t"),
        ("a", "ɑ"),
        ("y", "i"),
        ("ø", "ɜ"),
        ("œ", "ɛ"),
        ("ʏ", "ɪ"),
        ("ʁ", "ɹ"),
    ):
        ps = ps.replace(source, target)
    return ps


@cache
def bundled_fallback() -> "PronunciationFallback":
    """Verify once and share the hook and word cache across Voice Models.

    The data ships in the wheel and the app bundle, so a missing or altered
    file is an error, never input to the loaders.
    """
    verify_bundled_files()
    return PronunciationFallback(BUNDLED_DIRECTORY)


class WordToken(Protocol):
    text: str


# Misaki's own notion of a word: letters, with apostrophes inside ("O'Brien").
_LETTER_RUNS = re.compile(r"[^\W\d_]+(?:['‘’][^\W\d_]+)*")


class PronunciationFallback:
    """A Misaki hook with a word cache shared for its lifetime.

    The caller supplies verified data. Misaki hands over a merged compound
    ("well-Zyntrix", "Zyntrix2") whenever any part is unknown, so each run of
    letters is pronounced on its own. The generation worker is the sole
    caller, so no lock is needed.
    """

    def __init__(
        self,
        directory: Path,
        *,
        session_factory: Callable[[Path], Session] = onnx_session,
    ) -> None:
        self._lexicon: dict[str, str] = json.loads(
            (directory / "lexicon.json").read_text(encoding="utf-8")
        )
        self._session = session_factory(directory / "g2p.onnx")
        self._cache: dict[str, str] = {}

    def __call__(self, token: WordToken) -> tuple[str, int]:
        parts = [self._word(part) for part in _LETTER_RUNS.findall(token.text)]
        ps = " ".join(part for part in parts if part)
        return (ps, 2) if ps else ("", 0)

    def _word(self, word: str) -> str:
        from misaki import en

        word = unicodedata.normalize("NFKD", word.lower())
        if word in self._cache:
            return self._cache[word]
        ps = self._lexicon.get(word)
        if ps is None:
            ids = word_ids(word)
            # The exported dynamic graph was qualified for 3..256 tokens.
            if not 3 <= len(ids) <= 256:
                return ""
            logits = self._session.run(None, {"text": np.array([ids], dtype=np.int64)})[
                0
            ]
            ps = decode_logits(logits)
        ps = ipa_to_misaki(ps)
        if ps and not any(stress in ps for stress in en.STRESSES):
            ps = en.apply_stress(ps, 2)
        self._cache[word] = ps
        return ps
