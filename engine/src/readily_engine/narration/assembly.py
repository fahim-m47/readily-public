"""Segment assembly per ADR 0002 §4: trim, butt-join, pause, rarely crossfade.

Voice Models pad every utterance with silence of their own choosing, so the
pause a listener hears at a seam has to be authored here, as data from the
Catalog entry's tunables — never left to whatever the model emitted. Each
Segment is trimmed to its speech, then joined to the next sample-accurately:
a paragraph break gets the long pause, a sentence break the short one, and a
mid-sentence waterfall cut gets nothing at all — a butt-join, with a 10-20ms
equal-power crossfade reserved as the emergency treatment for that one seam
class, where two halves of a word can genuinely collide.

Pure NumPy over mono float PCM, no audio device and no model — so it tests
in full on ubuntu CI (engine/README.md § "Test layout").
"""

import numpy as np

from readily_engine.audio import FloatPcm
from readily_engine.audio.artifacts import SILENCE_RMS, frame_rms
from readily_engine.catalog import PausePolicy
from readily_engine.chunking import Boundary
from readily_engine.narration.timeline import TimelineWalker

# Silence is measured in the same 10ms frames as the crackle scrubber, so
# every part of the Engine agrees about where a pause starts.
_FRAME_SECONDS = 0.01


def trim_range(
    pcm: FloatPcm, sample_rate: int, *, lead_margin_ms: int, tail_margin_ms: int
) -> tuple[int, int]:
    """Locate the model's own leading and trailing padding off one Segment.

    Frames are judged against `SILENCE_RMS`; everything from the first
    active frame to the last stays, with a margin on each side for quiet
    releases and breaths. The margins are the pause policy's: version 1
    Narrations use zero to keep their original timeline, and a side that
    opens or closes a mid-sentence crossfade uses zero so the blend joins
    speech to speech. All-silent input trims to nothing.
    """
    frame_width = max(1, round(sample_rate * _FRAME_SECONDS))
    _frames, rms = frame_rms(pcm, frame_width)
    active = np.flatnonzero(rms > SILENCE_RMS)
    if not len(active):
        return 0, 0
    start = int(active[0]) * frame_width
    end = int(active[-1] + 1) * frame_width
    if int(active[-1]) == len(rms) - 1:
        end = len(pcm)
    lead = round(sample_rate * lead_margin_ms / 1000)
    tail = round(sample_rate * tail_margin_ms / 1000)
    return max(0, start - lead), min(len(pcm), end + tail)


def trim_silence(
    pcm: FloatPcm, sample_rate: int, *, lead_margin_ms: int, tail_margin_ms: int
) -> FloatPcm:
    """Trim a Segment using the same range persisted by Storage."""
    start, end = trim_range(
        pcm, sample_rate, lead_margin_ms=lead_margin_ms, tail_margin_ms=tail_margin_ms
    )
    return pcm[start:end]


def trim_margins(
    policy: PausePolicy, previous: Boundary | None, boundary: Boundary
) -> tuple[int, int]:
    """The (lead, tail) margins a Block between these two seams keeps, in ms.

    A mid-sentence seam is blended or butt-joined onto speech, so the side
    facing it keeps no margin.
    """
    margin = policy.trim_margin_ms
    lead = 0 if previous == Boundary.MID_SENTENCE else margin
    tail = 0 if boundary == Boundary.MID_SENTENCE else margin
    return lead, tail


# The ramp an exposed Segment edge gets. `trim_silence` cuts on frame
# boundaries, so a Segment can open or close part-way up a waveform, and the
# step between that sample and the silence either side of it is the hard edge
# qualification check B measures — a click, at the top of every Narration and
# around every authored pause. Three milliseconds is shorter than the onset of
# any phoneme and long enough that nothing steps.
EDGE_FADE_SECONDS = 0.003


def _fade_edge(pcm: FloatPcm, sample_rate: int, *, leading: bool) -> FloatPcm:
    """Ramp one end of a Segment up from, or down to, silence."""
    fade = min(round(sample_rate * EDGE_FADE_SECONDS), len(pcm))
    if fade < 2:
        return pcm
    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
    faded = pcm.copy()
    if leading:
        faded[:fade] *= ramp
    else:
        faded[-fade:] *= ramp[::-1]
    return faded


class Assembler:
    """Render the timeline walker's seam decisions on already trimmed mono PCM.

    Callers hand in Segments cut to their speech: playback and Export slice
    the range Storage measured, and whoever holds raw synthesis trims it
    first with `trim_margins` and `trim_silence`.
    """

    def __init__(
        self, policy: PausePolicy, *, timeline: TimelineWalker | None = None
    ) -> None:
        self.timeline = timeline if timeline is not None else TimelineWalker(policy)
        self._held_tail: FloatPcm | None = None
        self._last_lead_sec = 0.0
        self._last_lead_is_pause = False

    @property
    def last_lead_sec(self) -> float:
        """Length of the preceding pause or unblended tail in the last piece."""
        return self._last_lead_sec

    @property
    def last_lead_is_pause(self) -> bool:
        """Whether curation should assign the lead to authored silence."""
        return self._last_lead_is_pause

    def add(self, trimmed: FloatPcm, sample_rate: int, boundary: Boundary) -> FloatPcm:
        """Assemble an already measured Segment without trimming it again."""
        if sample_rate != self.timeline.sample_rate:
            self._held_tail = None
        seam = self.timeline.add(len(trimmed), sample_rate, boundary)
        pieces: list[FloatPcm] = []
        if seam.blend_frames:
            fade = seam.blend_frames
            ramp = np.linspace(0.0, np.pi / 2, fade, dtype=np.float32)
            if self._held_tail is None:
                # Resume has the predecessor's length, but never needs its PCM.
                blended = _fade_edge(trimmed[:fade], sample_rate, leading=True)
            else:
                blended = self._held_tail * np.cos(ramp) + trimmed[:fade] * np.sin(ramp)
            pieces.append(blended.astype(np.float32))
            trimmed = trimmed[fade:]
        elif self._held_tail is not None:
            pieces.append(self._held_tail)
        elif seam.lead_frames:
            pieces.append(np.zeros(seam.lead_frames, dtype=np.float32))
        self._held_tail = None
        if seam.opens_on_silence:
            trimmed = _fade_edge(trimmed, sample_rate, leading=True)
        if seam.hold_frames:
            self._held_tail = trimmed[-seam.hold_frames :]
            trimmed = trimmed[: -seam.hold_frames]
        pieces.append(trimmed)
        self._last_lead_sec = seam.lead_frames / sample_rate
        self._last_lead_is_pause = seam.lead_is_pause
        out = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
        return out if seam.hold_frames else _fade_edge(out, sample_rate, leading=False)

    def gap(self, boundary: Boundary) -> FloatPcm:
        """Resolve a failed Block as a break, releasing any preceding tail."""
        self.timeline.gap(boundary)
        return self._take_tail()

    def flush(self) -> FloatPcm:
        """Release the final tail without appending an authored pause."""
        self.timeline.flush()
        return self._take_tail()

    def _take_tail(self) -> FloatPcm:
        tail = self._held_tail
        self._held_tail = None
        if tail is None:
            return np.zeros(0, dtype=np.float32)
        return _fade_edge(tail, self.timeline.sample_rate, leading=False)
