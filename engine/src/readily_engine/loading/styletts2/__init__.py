"""The StyleTTS2 family: one synthesizer, parametrised by the export it runs.

`kokoro:82m` and `kitten-tts:15m` are two exports of the same architecture,
and everything one does that the other does not is a fact about its export
rather than a different pipeline: which name its graph gives the id row,
which symbols its table carries and how many it takes, whether its style
archive keeps a row per phoneme count, which English a Voice is phonemized
in, how Misaki's spellings map onto the symbols it was trained on, and
which repairs its draws need. `Spec` carries those facts, `kokoro.py` and
`kitten.py` fill one in each and register it as an Architecture (ADR 0014
§1), and `StyleTTS2Synthesizer` runs any of them: open the session and the
style archive, phonemize with the Misaki G2P the licence gate allows, look
the phonemes up, run the graph once, place the words from its duration
vector, repair the draw. A third export is another spec file.

The boundary to hold: a `Spec` names data and pure functions of one
argument, never a method that reaches back into the synthesizer. A spec
that grows methods is the old per-export module back under another name.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from readily_engine.audio import FloatPcm
from readily_engine.catalog import CatalogEntry
from readily_engine.generation import GeneratedAudio, GenerationRecord, Synthesizer
from readily_engine.loading.onnx import Session, onnx_session
from readily_engine.loading.pronunciation import WordToken, bundled_fallback
from readily_engine.loading.references import VoiceReferences
from readily_engine.timings import Timing, word_spans

Dialect = Literal["american", "british"]

# Every pinned export vocodes at 24 kHz, and `duration_timings` counts its
# duration vector in 600-sample frames of that rate.
SAMPLE_RATE = 24_000


class PhonemeToken(Protocol):
    text: str
    phonemes: str | None
    whitespace: str


# Text to Misaki's phoneme string and the tokens it was read from.
G2P = Callable[[str], tuple[str, Sequence[PhonemeToken]]]


def misaki_g2p(
    dialect: Dialect = "american",
    *,
    fallback: Callable[[WordToken], tuple[str, int]] | None = None,
) -> G2P:
    # A lookup rather than `dialect == "british"`, so a caller who passes
    # something else fails here instead of quietly getting an American G2P.
    # `fallback` is keyword-only for the same reason: a positional call would
    # otherwise be read silently as a dialect.
    british = {"american": False, "british": True}[dialect]

    from misaki import en

    return en.G2P(
        trf=False,
        british=british,
        fallback=bundled_fallback() if fallback is None else fallback,
        unk="",
    )


def token_ids(phonemes: str, vocab: Mapping[str, int], limit: int) -> list[int]:
    """The phonemes this export has a symbol for, as graph input ids.

    Symbols outside `vocab` are dropped rather than mapped to a pad id: an
    export's table is the set of symbols it was trained on, and feeding it
    an id it never saw is a worse answer than saying the sound with one
    phoneme fewer.
    """
    ids = [vocab[phone] for phone in phonemes if phone in vocab]
    if not ids:
        raise ValueError("Misaki produced no supported phonemes")
    if len(ids) > limit:
        raise ValueError(f"input exceeds the export's {limit}-phoneme limit")
    return ids


def sequence(ids: Sequence[int]) -> list[list[int]]:
    """One batch row, bracketed by the pad id every StyleTTS2 export reserves
    at index 0 — the boundary its duration predictor was trained to expect."""
    return [[0, *ids, 0]]


def timed_output(
    output: Sequence[npt.NDArray],
    text: str,
    tokens: Sequence[PhonemeToken],
    ids: Sequence[int],
    *,
    vocab: Mapping[str, int],
    respell: Callable[[str], str] = str,
) -> tuple[FloatPcm, tuple[Timing, ...]]:
    """The PCM a StyleTTS2 export drew, with the words its duration vector
    places when the export exposes that vector as output 1."""
    pcm = np.asarray(output[0], dtype=np.float32).ravel()
    durations = output[1] if len(output) > 1 else ()
    return pcm, duration_timings(
        text,
        tokens,
        ids,
        durations,
        vocab=vocab,
        sample_count=len(pcm),
        respell=respell,
    )


def duration_timings(
    text: str,
    tokens: Sequence[PhonemeToken],
    ids: Sequence[int],
    durations: Sequence[int],
    *,
    vocab: Mapping[str, int],
    sample_count: int,
    respell: Callable[[str], str] = str,
) -> tuple[Timing, ...]:
    """Map a complete token/id replay to words, or reject the whole Block.

    Both pinned exports emit one duration per padded input id, in units of
    600 samples at 24 kHz. Source offsets come from a forward-only cursor;
    repeated words and expanded numbers therefore keep their own position.
    """
    if (
        len(durations) != len(ids) + 2
        or any(not np.isfinite(d) or d <= 0 or int(d) != d for d in durations)
        or sum(durations) * 600 != sample_count
    ):
        return ()
    replay: list[int] = []
    spans: list[tuple[int, int, int, int]] = []
    cursor = 0
    for token in tokens:
        start = text.find(token.text, cursor)
        if not token.text or start < 0 or text[cursor:start].strip():
            return ()
        end = start + len(token.text)
        phones = [vocab[p] for p in respell(token.phonemes or "") if p in vocab]
        words = word_spans(token.text)
        if words:
            if len(words) != 1 or not phones:
                return ()
            left, right = words[0]
            spans.append(
                (
                    start + left,
                    start + right,
                    len(replay) + 1,
                    len(replay) + len(phones) + 1,
                )
            )
        replay.extend(phones)
        replay.extend(vocab[p] for p in respell(token.whitespace) if p in vocab)
        cursor = end
    if (
        replay != list(ids)
        or text[cursor:].strip()
        or [(a, b) for a, b, _, _ in spans] != word_spans(text)
    ):
        return ()
    edges = np.concatenate(([0], np.cumsum(durations))) / 40
    return tuple(
        Timing(
            start_char=a,
            end_char=b,
            start_sec=edges[left],
            end_sec=edges[right],
            provenance="spoken",
        )
        for a, b, left, right in spans
    )


@dataclass(frozen=True)
class Spec:
    """What one StyleTTS2 export is, as data the family synthesizer reads."""

    # How the export is named in what the Engine refuses.
    name: str
    # The two files the export is loaded from, relative to the promoted
    # directory: the graph and the `.npz` style archive keyed by Voice id.
    model_file: str
    voices_file: str
    # The graph input the padded id row is fed under; `style` and `speed`
    # are named the same on every spec.
    tokens_input: str
    # The symbols the export was trained on, by graph id, and the most it
    # takes in one row.
    vocab: Mapping[str, int]
    max_phonemes: int
    # Whether the archive keeps one style row per phoneme count, indexed by
    # how many the graph is about to speak, or one vector per Voice.
    style_by_length: bool
    # The English a Voice is phonemized in.
    dialect: Callable[[str], Dialect]
    # Misaki's spelling of a phoneme string, rewritten into the export's
    # own; `str` when the export was trained on Misaki's alphabet.
    respell: Callable[[str], str]
    # Whatever the export ships in every draw and the analyzer refuses,
    # taken back out; the last step before the audio leaves.
    repair: Callable[[FloatPcm], FloatPcm]


class StyleTTS2Synthesizer:
    """Load one export's graph and style archive once and synthesize English
    locally, in the dialect each Voice was trained on."""

    def __init__(
        self,
        model_dir: Path,
        spec: Spec,
        *,
        session_factory: Callable[[Path], Session] = onnx_session,
        g2p_factory: Callable[[Dialect], G2P] = misaki_g2p,
    ) -> None:
        model_path = model_dir / spec.model_file
        voices_path = model_dir / spec.voices_file
        if not model_path.is_file() or not voices_path.is_file():
            raise FileNotFoundError(f"no promoted {spec.name} model at {model_dir}")

        # The G2P verifies the bundled pronunciation data; fail on it before
        # paying for the Voice Model. The other dialect waits until a Voice
        # asks for it: each G2P is half a second and its own spaCy pipeline.
        self._spec = spec
        self._g2p_factory = g2p_factory
        self._g2p: dict[Dialect, G2P] = {"american": g2p_factory("american")}
        self._session = session_factory(model_path)
        with np.load(voices_path, allow_pickle=False) as archive:
            self._voices = {
                name: np.asarray(archive[name], dtype=np.float32)
                for name in archive.files
            }

    def generate(self, record: GenerationRecord) -> GeneratedAudio:
        spec = self._spec
        text, voice = record.text, record.voice_id
        if voice not in self._voices:
            raise ValueError(f"unknown {spec.name} voice: {voice}")
        dialect = spec.dialect(voice)
        if dialect not in self._g2p:
            self._g2p[dialect] = self._g2p_factory(dialect)
        phonemes, tokens = self._g2p[dialect](text)
        ids = token_ids(spec.respell(phonemes), spec.vocab, spec.max_phonemes)

        style = self._voices[voice]
        if spec.style_by_length:
            style = style[len(ids) - 1]
        output = self._session.run(
            None,
            {
                spec.tokens_input: np.asarray(sequence(ids), dtype=np.int64),
                "style": np.asarray(style, dtype=np.float32),
                "speed": np.asarray([1.0], dtype=np.float32),
            },
        )
        pcm, timings = timed_output(
            output, text, tokens, ids, vocab=spec.vocab, respell=spec.respell
        )
        return GeneratedAudio(spec.repair(pcm), SAMPLE_RATE, timings)


# A StyleTTS2 export samples nothing: a Voice and its text fix the audio.
class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class StyleTTS2Architecture:
    """One export's Architecture as the registry names it (ADR 0014)."""

    backend = "onnxruntime"
    conditioning = "preset"
    warmup_text = "Ready, Zyntrix."
    parameters = Parameters
    chunk_budget_candidates = None

    def __init__(self, spec: Spec) -> None:
        self.spec = spec

    def expected_files(self, entry: CatalogEntry) -> frozenset[str]:
        return frozenset({self.spec.model_file, self.spec.voices_file})

    def load(
        self, model_dir: Path, entry: CatalogEntry, *, references: VoiceReferences
    ) -> Synthesizer:
        return StyleTTS2Synthesizer(model_dir, self.spec)
