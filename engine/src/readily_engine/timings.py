"""Word coordinates in synthesis text and its accepted raw waveform."""

import re
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Provenance = Literal["spoken", "matched", "estimated"]
SEEK_LEAD_SECONDS: dict[Provenance, float] = {
    "spoken": 0.05,
    "matched": 0.15,
    "estimated": 0.25,
}
_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*")


def word_spans(text: str) -> list[tuple[int, int]]:
    """The Source words shared by synthesis mapping, seek and read-along."""
    return [word.span() for word in _WORD.finditer(text)]


class Timing(BaseModel):
    """Half-open character and second ranges, before any Narration trimming.

    Lanes must map expansions and internal chunks back to the Generation
    Record's text. A lane without that mapping returns an empty channel.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    start_char: int = Field(ge=0, strict=True)
    end_char: int = Field(gt=0, strict=True)
    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)
    provenance: Provenance

    @model_validator(mode="after")
    def ordered_ranges(self) -> Self:
        if self.end_char <= self.start_char or self.end_sec <= self.start_sec:
            raise ValueError("timing ranges must have positive length")
        return self


@dataclass(frozen=True)
class SourceTiming:
    source_start: int
    source_end: int
    start_sec: float
    end_sec: float
    provenance: Provenance


def place_timings(
    timings: tuple[Timing, ...],
    *,
    text: str,
    source_start: int,
    trim_start_sec: float,
    duration_sec: float,
    timeline_start_sec: float,
) -> tuple[SourceTiming, ...]:
    """Place accepted words and fill missing runs between anchors by character count.

    The seek path has no G2P: it also serves cached Segments with no loaded
    Voice Model. Block edges anchor the first and last missing runs.
    """
    words = tuple(_WORD.finditer(text))
    spans = {word.span() for word in words}
    accepted = {
        (timing.start_char, timing.end_char): timing
        for index, timing in enumerate(timings)
        if (timing.start_char, timing.end_char) in spans
        and timing.start_sec < trim_start_sec + duration_sec
        and not any(
            other_index != index
            and not (
                (
                    timing.end_char <= other.start_char
                    and timing.end_sec <= other.start_sec
                )
                or (
                    other.end_char <= timing.start_char
                    and other.end_sec <= timing.start_sec
                )
            )
            for other_index, other in enumerate(timings)
        )
    }
    placed: list[SourceTiming] = []
    cursor = 0
    left = 0.0
    for index in range(len(words) + 1):
        timing = accepted.get(words[index].span()) if index < len(words) else None
        if timing is None and index < len(words):
            continue
        right = max(0.0, timing.start_sec - trim_start_sec) if timing else duration_sec
        missing = words[cursor:index]
        weight = sum(word.end() - word.start() for word in missing)
        consumed = 0
        for word in missing:
            start = left + (right - left) * consumed / weight
            consumed += word.end() - word.start()
            end = left + (right - left) * consumed / weight
            placed.append(
                SourceTiming(
                    source_start + word.start(),
                    source_start + word.end(),
                    timeline_start_sec + start,
                    timeline_start_sec + end,
                    "estimated",
                )
            )
        if timing is not None:
            left = min(duration_sec, max(0.0, timing.end_sec - trim_start_sec))
            placed.append(
                SourceTiming(
                    source_start + timing.start_char,
                    source_start + timing.end_char,
                    timeline_start_sec + right,
                    timeline_start_sec + left,
                    timing.provenance,
                )
            )
        cursor = index + 1
    return tuple(placed)
