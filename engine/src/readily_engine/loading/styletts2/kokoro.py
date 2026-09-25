"""Kokoro v1.0 as a StyleTTS2 export (threat model B2).

Loads only from the path it is handed, which production wiring takes from
the store's promoted directory — a promoted directory exists ⇔ its contents
are hash-verified and complete (ADR 0003 §3). There is no default model
path: nothing here can reach files the store did not verify.

What is Kokoro's own: the dialect a Voice is phonemized in, a style archive
with a row per phoneme count, and a draw that comes off the graph ready to
play bar the clicks every export makes.
"""

# IPA characters are intentionally confusable with Latin letters.
# ruff: noqa: RUF001

from readily_engine.audio import FloatPcm
from readily_engine.audio.artifacts import declick
from readily_engine.loading.styletts2 import (
    SAMPLE_RATE,
    Dialect,
    Spec,
    StyleTTS2Architecture,
)

MAX_PHONEMES = 510
# The store's derivation of the pinned export, with the duration vector
# `/encoder/Gather_output_0` exposed as output 1 (ADR 0011).
MODEL_FILE = "kokoro-v1.0.timed.onnx"
VOICES_FILE = "voices-v1.0.bin"

# The vocabulary embedded in the qualified v1.0 export's source config. This
# ONNX predates exports that carry `kokoro_config` as graph metadata.
VOCAB = {
    ";": 1,
    ":": 2,
    ",": 3,
    ".": 4,
    "!": 5,
    "?": 6,
    "—": 9,
    "…": 10,
    '"': 11,
    "(": 12,
    ")": 13,
    "“": 14,
    "”": 15,
    " ": 16,
    "\u0303": 17,
    "ʣ": 18,
    "ʥ": 19,
    "ʦ": 20,
    "ʨ": 21,
    "ᵝ": 22,
    "\uab67": 23,
    "A": 24,
    "I": 25,
    "O": 31,
    "Q": 33,
    "S": 35,
    "T": 36,
    "W": 39,
    "Y": 41,
    "ᵊ": 42,
    "a": 43,
    "b": 44,
    "c": 45,
    "d": 46,
    "e": 47,
    "f": 48,
    "h": 50,
    "i": 51,
    "j": 52,
    "k": 53,
    "l": 54,
    "m": 55,
    "n": 56,
    "o": 57,
    "p": 58,
    "q": 59,
    "r": 60,
    "s": 61,
    "t": 62,
    "u": 63,
    "v": 64,
    "w": 65,
    "x": 66,
    "y": 67,
    "z": 68,
    "ɑ": 69,
    "ɐ": 70,
    "ɒ": 71,
    "æ": 72,
    "β": 75,
    "ɔ": 76,
    "ɕ": 77,
    "ç": 78,
    "ɖ": 80,
    "ð": 81,
    "ʤ": 82,
    "ə": 83,
    "ɚ": 85,
    "ɛ": 86,
    "ɜ": 87,
    "ɟ": 90,
    "ɡ": 92,
    "ɥ": 99,
    "ɨ": 101,
    "ɪ": 102,
    "ʝ": 103,
    "ɯ": 110,
    "ɰ": 111,
    "ŋ": 112,
    "ɳ": 113,
    "ɲ": 114,
    "ɴ": 115,
    "ø": 116,
    "ɸ": 118,
    "θ": 119,
    "œ": 120,
    "ɹ": 123,
    "ɾ": 125,
    "ɻ": 126,
    "ʁ": 128,
    "ɽ": 129,
    "ʂ": 130,
    "ʃ": 131,
    "ʈ": 132,
    "ʧ": 133,
    "ʊ": 135,
    "ʋ": 136,
    "ʌ": 138,
    "ɣ": 139,
    "ɤ": 140,
    "χ": 142,
    "ʎ": 143,
    "ʒ": 147,
    "ʔ": 148,
    "ˈ": 156,
    "ˌ": 157,
    "ː": 158,
    "ʰ": 162,
    "ʲ": 164,
    "↓": 169,
    "→": 171,
    "↗": 172,
    "↘": 173,
    "ᵻ": 177,
}


DIALECT_PREFIXES: dict[str, Dialect] = {"a": "american", "b": "british"}


def voice_dialect(voice_id: str) -> Dialect:
    """The English a Kokoro Voice was trained to speak.

    Kokoro names each preset for its language and dialect: the archive's own
    keys in `voices-v1.0.bin` start `af_`/`am_` for American English and
    `bf_`/`bm_` for British, and the Catalog's `language` field says the same
    thing. The archive's other prefixes are other languages, which this
    Engine has no phonemizer for, so they are refused rather than read aloud
    in English.
    """
    dialect = DIALECT_PREFIXES.get(voice_id[:1])
    if dialect is None:
        raise ValueError(f"Kokoro voice is not English: {voice_id}")
    return dialect


def declicked(pcm: FloatPcm) -> FloatPcm:
    cleaned, _clicks = declick(pcm, SAMPLE_RATE)
    return cleaned


SPEC = Spec(
    name="Kokoro",
    model_file=MODEL_FILE,
    voices_file=VOICES_FILE,
    tokens_input="tokens",
    vocab=VOCAB,
    max_phonemes=MAX_PHONEMES,
    # The archive carries one style row per supported phoneme count.
    style_by_length=True,
    dialect=voice_dialect,
    # Misaki writes the alphabet Kokoro was trained on.
    respell=str,
    repair=declicked,
)

ARCHITECTURE = StyleTTS2Architecture(SPEC)
