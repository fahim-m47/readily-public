"""The expressive Tier's MLX lane: load once, generate gated audio (threat model B2).

Loads only from the path it is handed — production wiring takes it from the
store's promoted directory (ADR 0003 §3). The MLX import stays lazy inside
this module so every other test runs on ubuntu; real MLX code paths belong
to the macOS smoke lane.

One lane serves every mlx-audio model. What differs per model is only how
its `generate` consumes the record — Qwen3-TTS takes a Voice or reference,
Chatterbox Turbo uses the Voice baked into the promoted directory — so the
call is a small function each Architecture owns (`loading/qwen3`,
`loading/chatterbox_turbo`) and hands to `MlxSynthesizer` together with its
runaway budget. Everything downstream of the call is shared.

A Voice the Catalog gives a reference clip is conditioned on that clip at
every Block instead of on a speaker name: the Base checkpoint's
speaker table is empty, so a name draws a new speaker per generation and
pitch, register and pace reset at every paragraph. The clip is decoded by
`loading.references` with the standard library and handed to the model as an
in-memory array.
Never a path: `ref_audio` as a path takes mlx-audio's own decoder, which is
the SciPy import ADR 0006 keeps out of the shipped Engine.

The lane returns a whole Block before the worker publishes its Segment. Decoder
streaming therefore buys no earlier playback. The Manifest selects the
mode; Qwen uses non-streaming while its streaming decoder starts each Block
without the reference's codec context.

The Architecture's runaway budget bounds every Block, including warm-up and
retries; the audio cutoff stops streamed runaways and removes long trailing
near-silence. The accept gate scores the first two
seconds of the finished PCM and rejects hf-heavy, voiceless draws. The
worker owns the one retry per Block.
The in-pause crackle scrubber sees the whole Block, including chunk seams.
No fixed onset trim is applied.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Protocol

import numpy as np

from readily_engine.audio import FloatPcm
from readily_engine.audio.artifacts import (
    HF8K_THRESHOLD,
    SILENCE_RMS,
    frame_rms,
    hf8k_ratio,
    scrub_noise_bursts,
    voiced_fraction,
)
from readily_engine.download.environment import hugging_face_offline
from readily_engine.generation import DegenerateDraw, GeneratedAudio, GenerationRecord
from readily_engine.loading.references import (
    NO_REFERENCES,
    VoiceReferenceAudio,
    VoiceReferences,
)

logger = logging.getLogger(__name__)

# Seconds of audio per streamed chunk (mlx-audio's streaming_interval): the
# ~2s cadence keeps chunks big enough to score and small enough that
# the first one lands at load + ~1s.
STREAMING_INTERVAL_SECONDS = 2.0

# HF8K_THRESHOLD is calibrated on whole generations; a 2s first chunk of
# clean speech tops it on sibilance alone (measured: up to 0.41 hf8k on an
# s-heavy opening, with a voiced fraction never under 0.30 across 36 clean
# draws). Broadband static carries no voiced body at all, so a draw is
# degenerate only when it is hf-heavy AND voiceless.
VOICED_SPEECH_FLOOR = 0.2

# The runaway cutoff's two triggers. Legit in-speech pauses top out at the
# entry's 400ms paragraph break, so seconds of unbroken near-silence can only
# be a missed end of speech; and 4 chars/s is ~3x slower than slow English
# reading, so a real generation never reaches the text-length budget.
RUNAWAY_SILENCE_SECONDS = 3.0
RUNAWAY_FLOOR_SECONDS = 5.0
RUNAWAY_MIN_CHARS_PER_SECOND = 4.0


def _trailing_silence(pcm: FloatPcm, sample_rate: int, carried: float) -> float:
    """Seconds of unbroken near-silence ending at this chunk's last sample,
    where `carried` is the run already trailing the previous chunk."""
    frame_width = sample_rate // 100
    _frames, rms = frame_rms(pcm, frame_width)
    if not len(rms):
        return carried + len(pcm) / sample_rate
    quiet = rms <= SILENCE_RMS
    if quiet.all():
        return carried + len(pcm) / sample_rate
    last_active = int(np.flatnonzero(~quiet)[-1])
    return (len(pcm) - (last_active + 1) * frame_width) / sample_rate


class GenerationResult(Protocol):
    audio: object
    sample_rate: int


type Generate[Model] = Callable[
    [Model, GenerationRecord, VoiceReferenceAudio | None], Iterator[GenerationResult]
]


def runaway_budget_seconds(text: str) -> float:
    """How long a Block for `text` may run before it is a runaway: the point
    at which the lane cuts any PCM, and the ceiling an Architecture that decodes in
    tokens may derive its token budget from."""
    return RUNAWAY_FLOOR_SECONDS + len(text) / RUNAWAY_MIN_CHARS_PER_SECOND


def _seed_rng(seed: int) -> None:
    import mlx.core as mx

    mx.random.seed(seed)


def _load[Model](model_dir: Path) -> Model:
    from mlx_audio.tts.utils import load

    # lazy=True defers weight materialization to first use: eager loading
    # peaks near 2.7GB RSS before settling, and the load peak is what
    # squeezes an 8GB machine.
    return load(model_dir, lazy=True)


class MlxSynthesizer[Model]:
    """Load a promoted mlx-audio model once and emit gated, scrubbed Blocks."""

    def __init__(
        self,
        model_dir: Path,
        *,
        generate: Generate[Model],
        runaway_seconds: Callable[[str], float],
        references: VoiceReferences = NO_REFERENCES,
        seed_rng: Callable[[int], None] = _seed_rng,
        model_factory: Callable[[Path], Model] = _load,
    ) -> None:
        if not (model_dir / "config.json").is_file():
            raise FileNotFoundError(f"no promoted MLX model at {model_dir}")
        # Loading must never be egress (threat model B2). mlx-audio 0.5.0's
        # Chatterbox Turbo `post_load_hook` tries `hf_hub_download` of an
        # unpinned `mlx-community/S3TokenizerV2`; offline, that fails locally,
        # is logged as a warning by mlx-audio, and leaves the speech tokenizer
        # randomly initialised. Narration never runs it: it conditions on
        # `ref_audio`, which the lane never passes to Chatterbox Turbo (ADR
        # 0009 amendment) — Qwen3-TTS gets one only as an in-memory array.
        with hugging_face_offline():
            self._model = model_factory(model_dir)
        self._references = references
        self._seed_rng = seed_rng
        self._generate_from = generate
        self._runaway_seconds = runaway_seconds

    def generate(self, record: GenerationRecord) -> GeneratedAudio:
        """Gate the finished Block's head before releasing any audio.

        The worker owns retries; each call draws from the saved seed.
        """
        with closing(self._generate(record)) as chunks:
            cutoffs: list[str] = []
            pieces = list(self._bounded(chunks, record.text, cutoffs))
        if not pieces:
            # A draw trimmed away as all silence is silent, not broken: the
            # worker redraws it like any other silent draw.
            raise DegenerateDraw("the model produced no audio")
        sample_rate = pieces[0][1]
        pcm = np.concatenate([piece for piece, _rate in pieces])
        head = pcm[: int(STREAMING_INTERVAL_SECONDS * sample_rate)]
        score = hf8k_ratio(head, sample_rate)
        if (
            score > HF8K_THRESHOLD
            and voiced_fraction(head, sample_rate) < VOICED_SPEECH_FLOOR
        ):
            raise DegenerateDraw(f"synthesis draw was degenerate (hf8k {score:.3f})")
        cleaned, _bursts = scrub_noise_bursts(pcm, sample_rate)
        return GeneratedAudio(cleaned, sample_rate, cutoffs=len(cutoffs))

    def _generate(self, record: GenerationRecord) -> Iterator[tuple[FloatPcm, int]]:
        reference = None
        if record.reference_digest is not None:
            key = (record.voice_id, record.reference_digest)
            if key not in self._references:
                raise LookupError(
                    f"no verified Voice Reference loaded for {record.voice_id!r} "
                    f"({record.reference_digest})"
                )
            reference = self._references[key]
        self._seed_rng(record.rng_seed)
        results = self._generate_from(self._model, record, reference)
        try:
            sample_rate = None
            for result in results:
                if result.sample_rate <= 0 or (
                    sample_rate is not None and sample_rate != result.sample_rate
                ):
                    raise ValueError("The synthesizer changed sample rate")
                sample_rate = result.sample_rate
                pcm = np.asarray(result.audio, dtype=np.float32).ravel()
                yield pcm, int(result.sample_rate)
        finally:
            close = getattr(results, "close", None)
            if close is not None:
                close()

    def _bounded(
        self, chunks: Iterator[tuple[FloatPcm, int]], text: str, cutoffs: list[str]
    ) -> Iterator[tuple[FloatPcm, int]]:
        """End a runaway generation instead of streaming it out: a missed
        end of speech pads toward the model's maximum length with
        near-silence and stray quiet junk that the hf8k gate cannot see."""
        budget = self._runaway_seconds(text)
        total = 0.0
        silence = 0.0
        for pcm, sample_rate in chunks:
            silence = _trailing_silence(pcm, sample_rate, silence)
            if silence >= RUNAWAY_SILENCE_SECONDS:
                cutoffs.append("silence")
                keep = len(pcm) - int(
                    min(silence, len(pcm) / sample_rate) * sample_rate
                )
                if keep > 0:
                    yield pcm[:keep], sample_rate
                logger.warning(
                    "runaway generation: %.1fs of unbroken near-silence — "
                    "ending the utterance",
                    silence,
                )
                return
            total += len(pcm) / sample_rate
            yield pcm, sample_rate
            if total >= budget:
                cutoffs.append("duration")
                logger.warning(
                    "runaway generation: %.1fs of audio from %d characters — "
                    "ending the utterance",
                    total,
                    len(text),
                )
                return
