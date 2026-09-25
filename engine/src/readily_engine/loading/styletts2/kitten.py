"""Kitten TTS Nano as a StyleTTS2 export (threat model B2).

The Catalog's smallest entry. Like Kokoro it is loaded only from the path
it is handed — production wiring takes that from the store's promoted
directory, and a promoted directory exists ⇔ its contents are hash-verified
and complete (ADR 0003 §3).

Upstream's own pipeline phonemizes with espeak through `phonemizer`, which is
GPL and deliberately absent from the locked environment. This export takes
Misaki's output instead, exactly as Kokoro does: the two are the same
StyleTTS2 family and take the same IPA, they only disagree about which
symbols their tables carry, which is what `as_espeak` reconciles.

What is Kitten's own: one style vector per Voice, a symbol table in
espeak-ng's spelling, and three defects in every draw that `repaired` takes
back out.
"""

# IPA characters are intentionally confusable with Latin letters.
# ruff: noqa: RUF001, RUF003

import numpy as np

from readily_engine.audio import FloatPcm
from readily_engine.audio.artifacts import (
    declick,
    remove_dc_drift,
    scrub_noise_bursts,
)
from readily_engine.loading.styletts2 import SAMPLE_RATE, Spec, StyleTTS2Architecture

MODEL_FILE = "kitten_tts_nano_v0_2.onnx"
VOICES_FILE = "voices.npz"

# Constant gain on every draw, because this export's vocoder overshoots full
# scale: measured over eight Voices and ten passages, `expr-voice-3-m` peaked
# at 1.17 and `expr-voice-3-f` at 1.01, which the float pipeline carries
# happily and the device output clips. A fixed trim rather than normalising
# each draw to its own peak — per-draw gain makes loudness follow content, and
# the ear hears that as pumping from one Block to the next. Sized so nothing
# under 1.25 clips — a fifth of headroom above the loudest draw seen.
HEADROOM = 0.8

# The export's graph takes a dynamic sequence length, so this limit is
# editorial rather than structural: a 15M-parameter duration predictor drifts
# out of step with the text well before it runs out of room, and the entry's
# `chunk_budget_chars` keeps real Blocks far below it. It is here so a
# pathological Block fails loudly instead of narrating something else.
MAX_PHONEMES = 600

# Upstream builds this table by position from four literal strings
# (`kittentts.onnx_model.TextCleaner`); it is transcribed rather than derived
# so the ids the graph is fed are reviewable here. The pinned revision ships
# only weights and voices, no tokenizer file, so the ids are the graph's own
# contract and a wrong one is silently a different sound. Nothing at runtime
# can catch that, so `test_the_symbol_table_agrees_with_upstreams_own_ordering`
# rebuilds those four strings by position and compares. Id 11 is unreachable
# in practice either way: Misaki curls every quote it emits (`He said "hello"`
# phonemizes to `“həlˈO”`), so a straight `"` never arrives and 14/15 carry
# the pair instead.
#
# The pad symbol `$` is left out on purpose: it is id 0, which `sequence`
# brackets the row with, and a `$` arriving mid-text is a pad token in the
# middle of a sentence rather than a sound. Id 174 is absent for the same
# structural reason it is absent upstream — the apostrophe appears twice in
# the IPA string, and by-position construction keeps the second.
VOCAB = {
    ";": 1,
    ":": 2,
    ",": 3,
    ".": 4,
    "!": 5,
    "?": 6,
    "¡": 7,
    "¿": 8,
    "—": 9,
    "…": 10,
    '"': 11,
    "«": 12,
    "»": 13,
    "“": 14,
    "”": 15,
    " ": 16,
    "A": 17,
    "B": 18,
    "C": 19,
    "D": 20,
    "E": 21,
    "F": 22,
    "G": 23,
    "H": 24,
    "I": 25,
    "J": 26,
    "K": 27,
    "L": 28,
    "M": 29,
    "N": 30,
    "O": 31,
    "P": 32,
    "Q": 33,
    "R": 34,
    "S": 35,
    "T": 36,
    "U": 37,
    "V": 38,
    "W": 39,
    "X": 40,
    "Y": 41,
    "Z": 42,
    "a": 43,
    "b": 44,
    "c": 45,
    "d": 46,
    "e": 47,
    "f": 48,
    "g": 49,
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
    "ɓ": 73,
    "ʙ": 74,
    "β": 75,
    "ɔ": 76,
    "ɕ": 77,
    "ç": 78,
    "ɗ": 79,
    "ɖ": 80,
    "ð": 81,
    "ʤ": 82,
    "ə": 83,
    "ɘ": 84,
    "ɚ": 85,
    "ɛ": 86,
    "ɜ": 87,
    "ɝ": 88,
    "ɞ": 89,
    "ɟ": 90,
    "ʄ": 91,
    "ɡ": 92,
    "ɠ": 93,
    "ɢ": 94,
    "ʛ": 95,
    "ɦ": 96,
    "ɧ": 97,
    "ħ": 98,
    "ɥ": 99,
    "ʜ": 100,
    "ɨ": 101,
    "ɪ": 102,
    "ʝ": 103,
    "ɭ": 104,
    "ɬ": 105,
    "ɫ": 106,
    "ɮ": 107,
    "ʟ": 108,
    "ɱ": 109,
    "ɯ": 110,
    "ɰ": 111,
    "ŋ": 112,
    "ɳ": 113,
    "ɲ": 114,
    "ɴ": 115,
    "ø": 116,
    "ɵ": 117,
    "ɸ": 118,
    "θ": 119,
    "œ": 120,
    "ɶ": 121,
    "ʘ": 122,
    "ɹ": 123,
    "ɺ": 124,
    "ɾ": 125,
    "ɻ": 126,
    "ʀ": 127,
    "ʁ": 128,
    "ɽ": 129,
    "ʂ": 130,
    "ʃ": 131,
    "ʈ": 132,
    "ʧ": 133,
    "ʉ": 134,
    "ʊ": 135,
    "ʋ": 136,
    "ⱱ": 137,
    "ʌ": 138,
    "ɣ": 139,
    "ɤ": 140,
    "ʍ": 141,
    "χ": 142,
    "ʎ": 143,
    "ʏ": 144,
    "ʑ": 145,
    "ʐ": 146,
    "ʒ": 147,
    "ʔ": 148,
    "ʡ": 149,
    "ʕ": 150,
    "ʢ": 151,
    "ǀ": 152,
    "ǁ": 153,
    "ǂ": 154,
    "ǃ": 155,
    "ˈ": 156,
    "ˌ": 157,
    "ː": 158,
    "ˑ": 159,
    "ʼ": 160,
    "ʴ": 161,
    "ʰ": 162,
    "ʱ": 163,
    "ʲ": 164,
    "ʷ": 165,
    "ˠ": 166,
    "ˤ": 167,
    "˞": 168,
    "↓": 169,
    "↑": 170,
    "→": 171,
    "↗": 172,
    "↘": 173,
    "̩": 175,
    "'": 176,
    "ᵻ": 177,
}

# Misaki writes American English in Kokoro's phoneme set rather than espeak's.
# Readily calls `misaki.en.G2P` with `fallback=None`, so ordinary text is
# resolved by Misaki's token and lexicon path, not `EspeakFallback`.
# `G2P.__call__` then applies its default-version final rewrite from `ɾ` to
# `T`. The same output alphabet uses `A`/`I`/`O`/`W`/`Y`, `ʤ`/`ʧ`, and `ᵊ`
# where espeak-ng writes different IPA spellings.
# `kokoro:82m` was trained on exactly that set and must keep it, but this
# export learned espeak-ng's own spelling, so Kitten needs the spellings below.
#
# Upstream's `_letters` carries the whole Latin alphabet and its IPA string
# carries the ligatures, so `A`, `T`, and `ʤ` *are* in the table above — at
# ids the export saw almost nothing at during training. They pass the
# `token_ids` guard and narrate a sound from nowhere: that is how "paste" came
# out as "pust", on every diphthong in the language, and how every j/ch/dge
# sound was voiced off its own id. `ᵊ` is the quiet half of the same bug: no
# id at all, so it was dropped without a sound.
#
# What this table cannot reach: Misaki also performs many-to-one rewrites, and
# the information needed to reverse them is not in its output.
# `EspeakFallback.E2M` maps `ɚ`→`əɹ`, `ɐ`→`ə`, `x`/`ç`→`k`, and `ɬ`→`l`;
# later steps strip every length mark, rewrite `ɜːɹ`/`ɜː`→`ɜɹ` and
# `ɪə`→`iə`, fold `ʔ`→`t`, and fold the syllabic diacritic into `ᵊ`
# (`n̩`→`ᵊn`) — which is why the `ᵊ`→`ə` below is right for `ə^l` and only
# approximate for the rest. Each target is a spelling espeak emits in its own
# right, so two
# preimages collapse onto one: `ə` is both `ɐ` and `ə`, `t` is both `ʔ` and
# `t`, and bare `i` is both `iː` and the unstressed happy vowel. The ids exist
# here — `ɐ` is 70, `ʔ` is 148, `ː` is 158, `ɚ` is 85 — so this is a real
# residue.
ESPEAK_SPELLING = {
    "A": "eɪ",
    "I": "aɪ",
    "O": "oʊ",
    "W": "aʊ",
    "Y": "ɔɪ",
    "T": "ɾ",
    "ʤ": "dʒ",
    "ʧ": "tʃ",
    "ᵊ": "ə",
}


def as_espeak(phonemes: str) -> str:
    """Rewrite Misaki's invertible spellings into what this export was trained on.

    Only Misaki's invertible output normalization is reversed; the many-to-one
    folds it also performs cannot be, and are noted at `ESPEAK_SPELLING`. The
    mapping is pinned symbol by symbol for
    `US_VOCAB` in `test_the_symbols_misaki_composes_are_spelled_back_out`.
    Synthetic `T` is covered by
    `test_misakis_synthetic_flap_goes_in_as_the_ipa_flap`, because membership
    in `VOCAB` cannot express "an id this export was trained on" — a missing
    entry leaves every id valid and every guard quiet.
    """
    return "".join(ESPEAK_SPELLING.get(phone, phone) for phone in phonemes)


def repaired(pcm: FloatPcm) -> FloatPcm:
    """Repair the three things this export ships in every draw.

    Kokoro needs only a declick; this export needs three repairs, and each
    step below is here because the analyzer refused the entry without it
    (`docs/adr/0008-model-weights-licence-policy.md` records the run).
    The order matters, because two of the three judge by absolute level.
    Drift first, for the scrubber's sake: it finds a pause by frame RMS,
    and a draw riding +0.05 has no frame under `SILENCE_RMS` anywhere, so
    the crackle in the model's own pauses is never looked for. (The
    declicker itself is indifferent — it differences the signal, so a bias
    is annihilated by construction.) Declick before the headroom trim,
    because its threshold is an absolute step size and a quieter draw
    hides steps under it. Scrub last, after the trim, because a gain
    change moves the line between pause and speech and the scrubber has
    to look at the levels the reader will hear.
    """
    levelled, _drift = remove_dc_drift(pcm, SAMPLE_RATE)
    declicked, _clicks = declick(levelled, SAMPLE_RATE)
    trimmed = (declicked * HEADROOM).astype(np.float32)
    scrubbed, _bursts = scrub_noise_bursts(trimmed, SAMPLE_RATE)
    return scrubbed


SPEC = Spec(
    name="Kitten TTS",
    model_file=MODEL_FILE,
    voices_file=VOICES_FILE,
    tokens_input="input_ids",
    vocab=VOCAB,
    max_phonemes=MAX_PHONEMES,
    # One style vector per Voice, whatever the length — unlike Kokoro,
    # whose archive carries a row per supported phoneme count.
    style_by_length=False,
    # Every Voice speaks American English.
    dialect=lambda _voice: "american",
    respell=as_espeak,
    repair=repaired,
)

ARCHITECTURE = StyleTTS2Architecture(SPEC)
