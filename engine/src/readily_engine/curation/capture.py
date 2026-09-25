"""Narrate one standard passage through a real Backend and keep the evidence.

Two artifacts come out of the same synthesis pass, which is why they are made
together rather than by two scripts that could disagree: the qualification
capture the analyzer reads (`audio/qualification.py`'s run layout), and the
Voice Preview clip the Catalog sheet auditions (ADR 0003 consequences).

The pass is the production pipeline, not a simplified copy of it: the same
chunker, the same per-Block synthesizer call, the same `Assembler`. A model
that only sounds right through a bespoke curation path is a model that will
not sound right in the app.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from readily_engine.audio import FloatPcm
from readily_engine.audio.artifacts import voiced_fraction
from readily_engine.audio.encoding import write_float_wav
from readily_engine.audio.qualification import CapturedSegment, write_run_manifest
from readily_engine.catalog import CatalogEntry, preview_clip_path
from readily_engine.chunking import Boundary, chunk
from readily_engine.curation.draft import CurationError
from readily_engine.generation import GenerationRecord, Synthesizer
from readily_engine.narration.assembly import Assembler, trim_margins, trim_silence

# The one passage every Voice narrates, so two entries are compared on the
# same words. Short enough to audition (about fifteen seconds), and shaped to
# exercise both authored pauses: two sentences inside a paragraph, then a
# paragraph break.
CAPTURE_PASSAGE = (
    "Readily reads what you paste, out loud, on your own Mac. "
    "Nothing you read leaves this machine, and nothing about it is uploaded "
    "anywhere.\n\n"
    "Pick a voice, press play, and the first words begin while the rest is "
    "still being written."
)


# The analyzer's checks are all absence-of-artifact: they catch noise, hard
# edges, discontinuities and crackle, and a Voice Model emitting pure silence
# trips none of them. This is the presence check that completes them — a
# fraction of *active* frames, so authored pauses do not dilute it. The two
# committed entries measure 0.82 and 0.88; broadband static sits near zero
# (`audio.artifacts.voiced_fraction`, and the same floor `loading/mlx_lane.py`
# gates a streamed draw with).
SPEECH_FLOOR = 0.2


def preview_relpath(entry: CatalogEntry, voice_id: str) -> str:
    """Where this Voice's clip lives under the bundled preview root
    (docs/voice-previews.md), which is the value the manifest carries.

    Validated here, not only by the schema that stores it: curation writes a
    file at this path, so a Voice id that could climb out of the preview
    root has to be refused before the write, not after it.
    """
    relative = f"{entry.name}/{entry.tag}/{voice_id}.m4a"
    try:
        return preview_clip_path(relative)
    except ValueError as error:
        raise CurationError(f"{entry.id} voice {voice_id!r}: {error}") from error


@dataclass(frozen=True)
class _Piece:
    """One Block's synthesis with its seam, as the `Assembler` resolved it:
    the raw draw, the assembled audio, and how much of that audio belongs to
    the seam behind it — an authored pause, or a held tail played out plain,
    which `lead_is_pause` tells apart."""

    raw: FloatPcm
    assembled: FloatPcm
    lead: int
    lead_is_pause: bool

    @property
    def opening(self) -> FloatPcm:
        return self.assembled[: self.lead]


def capture_voice(
    entry: CatalogEntry,
    voice_id: str,
    synthesizer: Synthesizer,
    run_dir: Path,
    passage: str = CAPTURE_PASSAGE,
) -> tuple[FloatPcm, int]:
    """Narrate `passage` in one Voice, writing an analyzer run and returning
    the assembled stream the Voice Preview is encoded from."""
    blocks = chunk(passage, entry.tunables).blocks
    if not blocks:
        raise CurationError("the capture passage produced no Blocks")

    assembler = Assembler(entry.tunables)
    pieces: list[_Piece] = []
    sample_rate = 0
    previous: Boundary | None = None
    for block in blocks:
        record = GenerationRecord.for_entry(entry, voice_id, block.text)
        try:
            result = synthesizer.generate(record)
        except ValueError as error:
            raise CurationError(
                f"the Backend could not narrate the passage as {voice_id!r}: {error}"
            ) from error
        raw, rate = result.pcm, result.sample_rate
        if sample_rate and rate != sample_rate:
            raise CurationError("the Backend changed sample rate mid-passage")
        sample_rate = rate
        lead_ms, tail_ms = trim_margins(entry.tunables, previous, block.boundary)
        trimmed = trim_silence(
            raw, rate, lead_margin_ms=lead_ms, tail_margin_ms=tail_ms
        )
        previous = block.boundary
        pieces.append(
            _Piece(
                raw=raw,
                assembled=assembler.add(trimmed, rate, block.boundary),
                lead=round(assembler.last_lead_sec * rate),
                lead_is_pause=assembler.last_lead_is_pause,
            )
        )
    tail = assembler.flush()

    if pieces[0].lead:
        raise CurationError("the first assembled piece opened with a seam")

    mute = [
        index
        for index, piece in enumerate(pieces)
        if len(piece.assembled) <= piece.lead
    ]
    if mute:
        raise CurationError(
            f"the Backend trimmed to nothing on Block(s) {mute} — it padded "
            f"silence where the passage has words"
        )

    stream = np.concatenate([*(piece.assembled for piece in pieces), tail]).astype(
        np.float32
    )
    voiced = voiced_fraction(stream, sample_rate)
    if voiced < SPEECH_FLOOR:
        raise CurationError(
            f"{voice_id} narrated the passage as {voiced:.2f} voiced speech, "
            f"under the {SPEECH_FLOOR:.2f} floor — the Backend is not speaking"
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    segments: list[CapturedSegment] = []
    start = 0
    for index, block in enumerate(blocks):
        piece = pieces[index]
        last = index == len(blocks) - 1
        # A piece opens with the seam behind it and ends where this Block's
        # speech ends; the analyzer's Segments are the other cut of the same
        # stream — speech first, then the pause that follows it — so each
        # Segment is this Block's speech plus the next piece's lead.
        speech = piece.assembled[piece.lead :]
        pause = np.zeros(0, dtype=np.float32)
        if last:
            speech = np.concatenate([speech, tail])
        elif pieces[index + 1].lead_is_pause:
            pause = pieces[index + 1].opening
        else:
            # The next piece's lead is not silence anyone authored: it is a
            # held tail the Assembler could not blend, played out plain, and
            # those samples are this Block's own speech. Counting them as a
            # pause would end `audio_len` mid-word, where check B reads a
            # step discontinuity as the Segment's edge — and check A's
            # clipping and DC statistics lose the tail.
            speech = np.concatenate([speech, pieces[index + 1].opening])
        processed = np.concatenate([speech, pause])

        write_float_wav(run_dir / f"seg{index:02d}-raw.wav", piece.raw, sample_rate)
        write_float_wav(
            run_dir / f"seg{index:02d}-processed.wav", processed, sample_rate
        )
        segments.append(
            CapturedSegment(
                i=index,
                sr=sample_rate,
                start=start,
                # `audio_len` is the analyzer's cut: checks A and B measure
                # the Segment over exactly `processed[:audio_len]`.
                # `pause_len` is descriptive — no check reads it.
                audio_len=len(speech),
                pause_len=len(pause),
                raw_len=len(piece.raw),
                raw_peak=float(np.max(np.abs(piece.raw))),
                text_chars=len(block.text),
                text=block.text,
            )
        )
        start += len(processed)

    write_run_manifest(
        run_dir,
        model=entry.id,
        voice=voice_id,
        stream_sr=sample_rate,
        segments=segments,
    )
    return stream, sample_rate


# How a Voice Preview is dressed before it is encoded. A clip is played from
# silence by a tap on a button, so it needs the head and tail room a Segment
# inside a Narration gets from its neighbours: `capture_voice` returns the
# stream trimmed hard to the speech, with only the Assembler's 3ms edge ramp
# (`narration.assembly.EDGE_FADE_SECONDS`) between silence and full voice,
# and an audio element starting or stopping on that steps audibly.
#
# 200ms in front is long enough for the output device to settle before the
# first phoneme and short enough that pressing Hear still feels immediate;
# 400ms behind lets the last word decay instead of being cut off. The fades
# are raised cosine rather than linear because a linear ramp still leaves a
# corner in the envelope: 15ms in is under the onset of any phoneme, and
# 80ms out is a deliberate release the ear reads as the clip ending rather
# than stopping.
HEAD_SILENCE_SECONDS = 0.2
TAIL_SILENCE_SECONDS = 0.4
FADE_IN_SECONDS = 0.015
FADE_OUT_SECONDS = 0.08


def _raised_cosine(length: int) -> FloatPcm:
    """A 0-to-1 ramp with no corner at either end: half a Hann window, open
    at the top so the sample after it is the first at full gain."""
    return (0.5 - 0.5 * np.cos(np.pi * np.arange(length) / length)).astype(np.float32)


def dress_for_audition(pcm: FloatPcm, sample_rate: int) -> FloatPcm:
    """Pad and fade a captured stream into the clip an Audition plays.

    Preview-only: it is applied at the encode site in `entry.curate`, never
    to the analyzer run `capture_voice` writes and never to a Narration —
    the Assembler's edges are ADR 0002's and qualification check B's, and
    dressing a Segment would move both.
    """
    body = np.asarray(pcm, dtype=np.float32).copy()
    if len(body):
        room = len(body) // 2
        fade_in = min(round(sample_rate * FADE_IN_SECONDS), room)
        fade_out = min(round(sample_rate * FADE_OUT_SECONDS), room)
        if fade_in > 1:
            body[:fade_in] *= _raised_cosine(fade_in)
        if fade_out > 1:
            body[-fade_out:] *= _raised_cosine(fade_out)[::-1]
    head = np.zeros(round(sample_rate * HEAD_SILENCE_SECONDS), dtype=np.float32)
    tail = np.zeros(round(sample_rate * TAIL_SILENCE_SECONDS), dtype=np.float32)
    return np.concatenate([head, body, tail])
