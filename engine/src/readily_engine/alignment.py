"""Align known synthesis text to CTC posteriors without a transcription runtime."""

from collections.abc import Callable
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from readily_engine.audio.resampling import resample_poly
from readily_engine.timings import Timing, word_spans

PCM = NDArray[np.float32]
FRAME_SECONDS = 0.02
TIMING_CONFIDENCE_THRESHOLD = 0.8


class Aligner(Protocol):
    def align(self, text: str, pcm: PCM, sample_rate: int) -> tuple[Timing, ...]: ...


class CTCAligner:
    """Match a transcript using a 16 kHz, 20 ms CTC acoustic model.

    The injected inference function owns artifact-specific preprocessing and
    returns frame-by-label log probabilities. Unsupported words refuse the
    Segment: dropping them would let neighbouring words align to their audio.
    """

    def __init__(self, infer: Callable[[PCM], PCM], labels: tuple[str, ...]) -> None:
        self._infer = infer
        self._labels = {label: index for index, label in enumerate(labels)}
        self._blank = self._labels["<pad>"]

    def align(self, text: str, pcm: PCM, sample_rate: int) -> tuple[Timing, ...]:
        words = word_spans(text)
        if not words or len(pcm) < sample_rate * 0.025:
            return ()
        tokens: list[int] = []
        spans: list[tuple[int, int]] = []
        for start_char, end_char in words:
            normalized = text[start_char:end_char].upper().replace("\u2019", "'")
            if any(letter not in self._labels for letter in normalized):
                return ()
            if tokens:
                tokens.append(self._labels["|"])
            start = len(tokens)
            tokens.extend(self._labels[letter] for letter in normalized)
            spans.append((start, len(tokens)))
        log_probs = self._infer(resample_poly(pcm, sample_rate, 16000))
        if (
            log_probs.ndim != 2
            or log_probs.shape[1] != len(self._labels)
            or not np.isfinite(log_probs).all()
        ):
            raise ValueError("the aligner returned invalid CTC posteriors")
        path = _viterbi(log_probs, tokens, self._blank)
        if path is None:
            return ()
        states = np.full(2 * len(tokens) + 1, self._blank, dtype=np.int32)
        states[1::2] = tokens
        speech = log_probs.argmax(axis=1) != self._blank
        if speech.any():
            chosen = log_probs[np.arange(len(path)), states[path]]
            explained = np.exp(chosen[speech] - log_probs[speech].max(axis=1)).mean()
            if explained < TIMING_CONFIDENCE_THRESHOLD:
                return ()
        timings = []
        for word, (first, last) in zip(words, spans, strict=True):
            frames = [
                np.flatnonzero(path == 2 * token + 1) for token in range(first, last)
            ]
            confidence = float(
                np.exp(
                    np.concatenate(
                        [
                            log_probs[indices, tokens[token]]
                            for token, indices in zip(
                                range(first, last), frames, strict=True
                            )
                        ]
                    ).mean()
                )
            )
            if confidence < TIMING_CONFIDENCE_THRESHOLD:
                continue
            timings.append(
                Timing(
                    start_char=word[0],
                    end_char=word[1],
                    start_sec=float(frames[0][0]) * FRAME_SECONDS,
                    end_sec=min(
                        float(frames[-1][-1] + 1) * FRAME_SECONDS,
                        len(pcm) / sample_rate,
                    ),
                    provenance="matched",
                )
            )
        return tuple(timings)


def _viterbi(log_probs: PCM, tokens: list[int], blank: int) -> NDArray[np.int32] | None:
    """Find a complete CTC path, including blanks between repeated letters."""
    states = np.full(2 * len(tokens) + 1, blank, dtype=np.int32)
    states[1::2] = tokens
    skip = np.zeros(len(states), dtype=bool)
    skip[2:] = (states[2:] != blank) & (states[2:] != states[:-2])
    previous = np.full(len(states), -np.inf, dtype=np.float32)
    previous[0] = 0
    back = np.zeros((len(log_probs), len(states)), dtype=np.uint8)
    for frame, emissions in enumerate(log_probs):
        choices = np.full((3, len(states)), -np.inf, dtype=np.float32)
        choices[0] = previous
        choices[1, 1:] = previous[:-1]
        choices[2, 2:] = np.where(skip[2:], previous[:-2], -np.inf)
        back[frame] = choices.argmax(axis=0)
        previous = choices.max(axis=0) + emissions[states]
    state = len(states) - 2 + int(previous[-1] > previous[-2])
    if not np.isfinite(previous[state]):
        return None
    path = np.empty(len(log_probs), dtype=np.int32)
    for frame in range(len(log_probs) - 1, -1, -1):
        path[frame] = state
        state -= int(back[frame, state])
    return path
