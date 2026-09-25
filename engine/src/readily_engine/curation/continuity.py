"""Qualify a complete recipe on the continuity passage through production synthesis."""

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from readily_engine.audio import FloatPcm
from readily_engine.audio.artifacts import frame_rms
from readily_engine.audio.encoding import write_float_wav
from readily_engine.catalog import CatalogEntry
from readily_engine.catalog.recipes import recipe_digest
from readily_engine.chunking import Boundary, chunk
from readily_engine.curation.draft import CurationError
from readily_engine.generation import GenerationRecord, Synthesizer
from readily_engine.narration.assembly import Assembler, trim_margins, trim_silence

F0_MIN_HZ, F0_MAX_HZ = 60.0, 400.0
WALK_BUDGET_HZ = 15.0

BLOCKS = (
    "The town of Harrow sat where the river slowed and widened, and for most "
    "of the year nothing much happened there. Boats came and went. The bakery "
    "opened at six. The clock on the church tower ran four minutes fast, and "
    "everyone had long since stopped correcting for it.",
    "Mara had lived on the hill road her whole life, in the same grey house "
    "with the same crooked gate. She knew the sound of every neighbour's "
    "door and could tell the time by which chimney was smoking. It was a "
    "small life, and she had never thought to want a larger one.",
    "That changed on a Tuesday in late October, when a letter arrived with no "
    "return address. The paper was thick and cream coloured, and the "
    "handwriting leaned hard to the right, as if the writer had been in a "
    "hurry or a temper. It was addressed to her by her full name.",
    "She read it standing in the hallway with her coat still on. Then she "
    "read it again, sitting down. The letter said that her aunt Iris, whom "
    "she had not seen in twenty years, had died, and had left her a house on "
    "the coast and everything in it.",
    "Mara did not remember much about Iris. A tall woman with silver rings on "
    "every finger, who laughed too loudly at dinner and once let her stay up "
    "past midnight to watch a meteor shower from the roof. After that summer "
    "there had been a falling out, and the visits stopped.",
    "The house was called Saltmarsh, and the solicitor's note gave directions "
    "that involved two trains, a bus, and a walk of about a mile along an "
    "unmarked track. Mara packed a single bag. She told the bakery she would "
    "be gone a week, and she locked the crooked gate behind her.",
    "The journey took most of a day. The second train was nearly empty, and "
    "she spent it watching fields give way to marsh and marsh give way to a "
    "flat silver line she slowly understood was the sea. She had not seen it "
    "since she was a child. It was bigger than she remembered.",
    "The bus driver knew the house. Everyone out there did, he said, though "
    "nobody went near it now. He dropped her at a bend in the road where the "
    "track began, wished her luck in a tone she could not quite read, and "
    "drove off before she could ask what he meant.",
    "Saltmarsh stood alone at the end of the track, three storeys of "
    "weathered stone with a slate roof and tall windows that caught the last "
    "of the light. The garden had gone wild. A rowing boat lay upside down "
    "in the long grass, its paint flaking to nothing.",
    "The key turned easily, which surprised her. Inside, the air was cold and "
    "smelled of salt and old paper. Dust sheets covered the furniture in the "
    "front room, and someone, not long ago, had left a single cup on the "
    "kitchen table, washed and turned upside down to dry.",
    "She found the study on the first floor. Books lined three walls, floor "
    "to ceiling, and a fourth wall was given over to maps, hand drawn and "
    "pinned in overlapping layers. Every one of them showed the same stretch "
    "of coast, and every one was different.",
    "On the desk lay a notebook, open, with a pen resting in the fold. The "
    "last entry was dated the week before Iris died. It read, in the same "
    "leaning hand as the letter: the tide will be right on the fourth. If "
    "she comes, she will know what to do.",
    "Mara sat down heavily in the desk chair. She did not know what to do. "
    "She did not know what the tide had to do with anything, or why her "
    "aunt, who had not written in two decades, had been so certain she would "
    "come. Outside, the light was going, and the wind was picking up.",
    "She slept badly, in a bed that was too soft, listening to the house "
    "settle and the sea work at the shingle below the garden. Twice she "
    "thought she heard footsteps on the stairs. Both times, when she made "
    "herself get up and look, there was no one there.",
    "Morning brought a clear sky and a low tide, and with them a kind of "
    "courage. She made tea in the cold kitchen, found a pair of boots by the "
    "back door that fit her almost exactly, and went out to see what the "
    "maps had been trying to say.",
    "The path down to the beach was steep and half washed away. At the "
    "bottom, the sand stretched out farther than seemed possible, wet and "
    "shining, and far out at the edge of it something dark stood up against "
    "the sky. A post, she thought at first. Then a mast.",
    "It was a boat, or what was left of one, sunk to its gunwales in the "
    "sand. The timbers were black with age. Someone had lashed a rope to the "
    "stump of the mast and run it back toward the shore, and the rope, "
    "unlike the boat, was new.",
    "She followed the rope. It led to a stake driven into the sand just above "
    "the tide line, and tied to the stake was a tin box, sealed with wax. "
    "Inside, wrapped in oilcloth, was a second notebook, and a photograph of "
    "two girls on a roof, squinting up at the night sky.",
    "Mara knew the photograph. She was the smaller of the two girls. The "
    "other was not Iris but a cousin she had somehow forgotten, a thin, "
    "serious child called Wren, who had been there that summer and then, "
    "after it, had never been mentioned again.",
    "She stood on the wet sand with the tide beginning to turn behind her and "
    "understood, slowly, that she had not been left a house. She had been "
    "left a question. And whatever the answer was, it had been waiting out "
    "here, at the edge of the map, for twenty years.",
)


def f0_track(pcm: FloatPcm, sample_rate: int) -> np.ndarray:
    """Per-frame fundamental of the voiced frames, by normalised
    autocorrelation over 50 ms frames. Coarse — a median over a Block is
    what the tool reports, and the ear is the judge behind it."""
    frame = sample_rate // 20
    hop = frame // 2
    lo, hi = int(sample_rate // F0_MAX_HZ), int(sample_rate // F0_MIN_HZ)
    out: list[float] = []
    for start in range(0, len(pcm) - frame, hop):
        segment = pcm[start : start + frame].astype(np.float64)
        if np.sqrt(np.mean(segment**2)) < 0.02:
            continue
        segment -= segment.mean()
        ac = np.correlate(segment, segment, "full")[frame - 1 :]
        ac /= ac[0] + 1e-9
        window = ac[lo:hi]
        best = float(window.max())
        if best <= 0.5:
            continue
        # Prefer the shortest lag among near-equal peaks: a period doubled by
        # a strong subharmonic scores about as well, and would halve the pitch.
        peaks = np.flatnonzero(
            (window[1:-1] >= window[:-2])
            & (window[1:-1] >= window[2:])
            & (window[1:-1] >= 0.9 * best)
        )
        lag = lo + 1 + int(peaks[0]) if peaks.size else lo + int(window.argmax())
        out.append(sample_rate / lag)
    return np.array(out)


def walk(f0_medians: list[float]) -> float:
    """The largest departure of any Block's median pitch from Block 1's."""
    if not f0_medians:
        return 0.0
    first = f0_medians[0]
    return max(abs(f0 - first) for f0 in f0_medians)


@dataclass(frozen=True)
class BlockEvidence:
    ordinal: int
    record: str
    f0_hz: float | None
    tail_dbfs: float | None
    peak: float


@dataclass(frozen=True)
class RecipeReport:
    recipe_sha256: str
    blocks: tuple[BlockEvidence, ...]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def check_recipe(
    entry: CatalogEntry,
    voice_id: str,
    synthesizer: Synthesizer,
    out: Path,
) -> RecipeReport:
    """Capture every Block; refuse missing speech, walking pitch and clipped tails.

    Acoustic acceptance is necessary but a curator must also listen to joined.wav.
    A fresh directory prevents interrupted runs from leaving stale passing evidence.
    """
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise CurationError("qualification needs an empty capture directory")
    blocks = chunk("\n\n".join(BLOCKS), entry.tunables).blocks
    assembler = Assembler(entry.tunables)
    evidence = []
    failures = []
    pieces = []
    rate = None
    previous = None
    for index, block in enumerate(blocks):
        record = GenerationRecord.for_entry(entry, voice_id, block.text)
        result = synthesizer.generate(record)
        if rate is not None and result.sample_rate != rate:
            raise CurationError("sample rate changed during qualification")
        rate = result.sample_rate
        raw = result.pcm
        lead, tail = trim_margins(entry.tunables, previous, block.boundary)
        pcm = trim_silence(raw, rate, lead_margin_ms=lead, tail_margin_ms=tail)
        pitches = f0_track(pcm, rate)
        pitch = float(np.median(pitches)) if len(pitches) else None
        peak = float(np.max(np.abs(raw)))
        # The ending is measured before Pause Policy margins: appended
        # silence must not hide a word cut off near speech level.
        ending = trim_silence(raw, rate, lead_margin_ms=0, tail_margin_ms=0)
        _, levels = frame_rms(ending, rate // 100)
        rms = float(levels[-1]) if levels.size else 0
        dbfs = 20 * math.log10(rms) if rms > 0 else None
        if pitch is None:
            failures.append(f"Block {index}: no measurable voiced speech")
        if peak >= 1 or (
            block.boundary != Boundary.MID_SENTENCE and dbfs is not None and dbfs > -36
        ):
            failures.append(f"Block {index}: clipped ending or saturated audio")
        evidence.append(
            BlockEvidence(index, record.canonical_json(), pitch, dbfs, peak)
        )
        write_float_wav(out / f"block-{index:03}.wav", raw, rate)
        pieces.append(assembler.add(pcm, rate, block.boundary))
        previous = block.boundary
    pieces.append(assembler.flush())
    pitches = [block.f0_hz for block in evidence if block.f0_hz is not None]
    if walk(pitches) > WALK_BUDGET_HZ:
        failures.append(
            "walking Voice: pitch departs more than 15 Hz from the first Block"
        )
    report = RecipeReport(
        recipe_digest(entry, voice_id),
        tuple(evidence),
        tuple(failures),
    )
    write_float_wav(out / "joined.wav", np.concatenate(pieces).astype(np.float32), rate)
    (out / "qualification.json").write_text(
        json.dumps(asdict(report), indent=2, allow_nan=False) + "\n"
    )
    return report
