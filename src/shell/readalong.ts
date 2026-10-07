// The Source as something a reader can follow and click into: given the
// Engine's playhead, which Block is a reader hearing right now?
//
// The Engine owns the playhead and derives Block starts from cached lengths
// and the Narration's pause policy. A Block's `timeline` stays null when an
// earlier Block's length is still unknown.

import type { HistoryGap, HistoryNarration, SourceTiming } from "../engine/client";
import { formatDuration } from "./estimate";
import { markGaps, type SourcePiece } from "./history";
import { sourceText, type SourceText } from "./source";

export type ReadAlongBlock = {
  // The Engine's own Block ordinal, or `null` for Source it has not cut
  // into a Block yet — the tail of a Narration that is still being read.
  ordinal: number | null;
  // Where this Block's text starts in the Source. Stable across refetches
  // in a way an array index is not, so it is what React keys on.
  start: number;
  // The Block's text, split into the runs that were read and the runs that
  // were skipped, via `markGaps` — every character exactly once (ADR 0002 §8).
  pieces: SourcePiece[];
  // The same pieces cut into the words a reader can click into. Cut here,
  // once per Block, rather than by the view: at the Source cap this is a
  // quarter-million word runs, and a Narration being read for the first
  // time is refetched every time a Block is assembled.
  readPieces: ReadPiece[];
  // Where this Block sits on the playhead's timeline and, in `sourceOffset`,
  // the Engine's Source coordinate to seek to — excluding inter-Block
  // whitespace. `null` while the Engine cannot derive the start yet, and a
  // Block without one cannot be highlighted yet.
  timeline: { sourceOffset: number; startSec: number } | null;
  // A word seek may prepare this Block once its preceding lengths are known.
  seekSourceOffset: number | null;
  skipped: boolean;
  timings: SourceTiming[];
  // The inputs the fields above were derived from and do not keep: the
  // Source, where the Block ends, the gaps that overlap it and the Segment's
  // timings before the range filter. A later cut with the same inputs hands
  // back this very object, and the memoized view skips it.
  cut: { source: string; end: number; gaps: HistoryGap[]; timings: SourceTiming[] };
};

// Cuts a Narration's Source into the Blocks the Engine made of it.
//
// The Segment ranges are the Engine's, so they are clamped and ordered here
// rather than trusted — and each Block runs from where the last one ended,
// not from its own `sourceStart`, so the whitespace between two Blocks goes
// to the Block that follows and the rendered text stays identical to the
// Source the reader pasted.
//
// Text past the last Segment becomes one trailing Block with no ordinal and
// no timeline: words the Engine has not reached, so not seekable.
//
// Given `previous`, an earlier cut, a Block whose inputs have not changed
// comes back as the same object. Inputs are compared by value, not by
// identity: a refetch while the Narration is being read hands the Segments
// over as fresh but equal objects, and the whole cut is what a reused
// Block saves.
export const readAlongBlocks = (
  { source, segments, gaps }: Pick<HistoryNarration, "source" | "segments" | "gaps">,
  previous: readonly ReadAlongBlock[] = [],
): ReadAlongBlock[] => {
  const text = sourceText(source);
  const clamp = (offset: number) => Math.min(Math.max(offset, 0), text.length);
  // The Source is compared once: a million-character string equal by value
  // but not by reference would otherwise be walked once per Block.
  const reusable = new Map(
    previous.length > 0 && previous[0].cut.source === source
      ? previous.map((block) => [block.start, block] as const)
      : [],
  );

  const ordered = segments
    .map((segment) => {
      const start = clamp(segment.sourceStart);
      return {
        ordinal: segment.ordinal,
        measured: segment.durationSec !== null,
        end: clamp(segment.sourceEnd),
        start,
        timings: segment.timings ?? [],
        timeline:
          segment.timelineStartSec === null
            ? null
            : { sourceOffset: start, startSec: segment.timelineStartSec },
      };
    })
    .sort((left, right) => left.start - right.start || left.ordinal - right.ordinal);

  const place = (
    start: number,
    end: number,
    placement: Pick<ReadAlongBlock, "ordinal" | "timeline" | "timings" | "seekSourceOffset">,
  ) => {
    const cut = {
      source,
      end,
      gaps: gaps.filter((gap) => gap.sourceEnd > start && gap.sourceStart < end),
      timings: placement.timings,
    };
    const kept = reusable.get(start);
    return kept !== undefined &&
      kept.ordinal === placement.ordinal &&
      kept.seekSourceOffset === placement.seekSourceOffset &&
      kept.timeline?.sourceOffset === placement.timeline?.sourceOffset &&
      kept.timeline?.startSec === placement.timeline?.startSec &&
      sameCut(kept.cut, cut)
      ? kept
      : blockAt(text, start, placement, cut);
  };

  const blocks: ReadAlongBlock[] = [];
  let cursor = 0;
  let prefixKnown = true;
  const gapOrdinals = new Set(gaps.map(gap => gap.ordinal));
  for (const segment of ordered) {
    const seekSourceOffset = segment.timeline?.sourceOffset ?? (prefixKnown ? segment.start : null);
    prefixKnown &&= segment.measured || gapOrdinals.has(segment.ordinal);
    // Already emitted by the Block that overlapped it.
    if (segment.end <= cursor) continue;
    blocks.push(place(cursor, segment.end, { ...segment, seekSourceOffset }));
    cursor = segment.end;
  }
  if (cursor < text.length) {
    blocks.push(place(cursor, text.length, { ordinal: null, timeline: null, timings: [], seekSourceOffset: null }));
  }

  return blocks;
};

const sameCut = (kept: ReadAlongBlock["cut"], next: ReadAlongBlock["cut"]) =>
  kept.end === next.end &&
  sameList(kept.gaps, next.gaps, (gap, other) =>
    gap.ordinal === other.ordinal && gap.sourceStart === other.sourceStart && gap.sourceEnd === other.sourceEnd) &&
  sameList(kept.timings, next.timings, (word, other) =>
    word.sourceStart === other.sourceStart &&
    word.sourceEnd === other.sourceEnd &&
    word.startSec === other.startSec &&
    word.endSec === other.endSec &&
    word.provenance === other.provenance);

const sameList = <T>(kept: readonly T[], next: readonly T[], same: (kept: T, next: T) => boolean) =>
  kept === next || (kept.length === next.length && kept.every((item, index) => same(item, next[index])));

const blockAt = (
  source: SourceText,
  start: number,
  placement: Pick<ReadAlongBlock, "ordinal" | "timeline" | "timings" | "seekSourceOffset">,
  cut: ReadAlongBlock["cut"],
): ReadAlongBlock => {
  const { end } = cut;
  // Gap ranges are the Source's; `markGaps` works in the Block's and clamps
  // whatever falls outside.
  const pieces = markGaps(
    source.slice(start, end),
    cut.gaps.map((gap) => ({
      ...gap,
      sourceStart: gap.sourceStart - start,
      sourceEnd: gap.sourceEnd - start,
    })),
  );
  const timings = placement.timings.filter(word => word.sourceStart >= start && word.sourceEnd <= end);

  return {
    ordinal: placement.ordinal,
    start,
    pieces,
    readPieces: readPieces({ start, pieces, timings }),
    timeline: placement.timeline,
    seekSourceOffset: placement.seekSourceOffset,
    timings,
    skipped: pieces.some((piece) => piece.skipped),
    cut,
  };
};

// Which Block the playhead is inside: the last one that has started.
//
// `-1` when no Block has a known start at or before the playhead.
export const activeBlockIndex = (
  blocks: readonly ReadAlongBlock[],
  positionSec: number,
) => {
  let active = -1;
  let started = -1;
  blocks.forEach((block, index) => {
    const { timeline } = block;
    if (timeline === null || timeline.startSec > positionSec) return;
    if (timeline.startSec >= started) {
      started = timeline.startSec;
      active = index;
    }
  });
  return active;
};

// The player's clock: a position — the playhead, or the thumb while it is
// held — out of the Narration's length. `totalSec` grows as the Engine
// assembles, and the position is clamped to it so a playhead arriving a
// snapshot ahead of the total never reads past the end.
export const formatPlayhead = (positionSec: number, totalSec: number) => {
  const total = Math.max(totalSec, 0);
  return [
    formatDuration(Math.min(Math.max(positionSec, 0), total)),
    formatDuration(total),
  ] as const;
};

export type SourceRun = {
  text: string;
  sourceOffset: number;
  timing: SourceTiming | null;
  word: boolean;
};

// One of a Block's pieces placed in the Source, with its words cut out
// where it was read. A skipped piece has no runs: a gap is marked, not
// clicked into.
export type ReadPiece = SourcePiece & { sourceOffset: number; runs: SourceRun[] };

// What the Engine also counts as a word (`timings.py`): letters and digits,
// joined across an apostrophe. Anything else is the text between words.
export const WORD = /([\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)*)/u;

// A Block's pieces as the runs the reader can click into, in Source order
// and with every character exactly once: each timed word, each word the
// Engine has not timed yet, and the text between them.
export const readPieces = (block: Pick<ReadAlongBlock, "start" | "pieces" | "timings">): ReadPiece[] => {
  let cursor = block.start;
  return block.pieces.map((piece) => {
    const text = sourceText(piece.text);
    const sourceOffset = cursor;
    cursor += text.length;
    return {
      ...piece,
      sourceOffset,
      runs: piece.skipped ? [] : runsIn(text, sourceOffset, block.timings),
    };
  });
};

const runsIn = (text: SourceText, start: number, timings: readonly SourceTiming[]) => {
  const runs: SourceRun[] = [];
  const end = start + text.length;
  let at = start;
  for (const timing of timings) {
    if (timing.sourceStart < at || timing.sourceEnd <= timing.sourceStart || timing.sourceEnd > end) continue;
    runs.push(...untimedRuns(text.slice(at - start, timing.sourceStart - start), at));
    runs.push({
      text: text.slice(timing.sourceStart - start, timing.sourceEnd - start),
      sourceOffset: timing.sourceStart,
      timing,
      word: true,
    });
    at = timing.sourceEnd;
  }
  runs.push(...untimedRuns(text.slice(at - start), at));
  return runs;
};

const untimedRuns = (text: string, start: number): SourceRun[] => {
  let sourceOffset = start;
  return text
    .split(WORD)
    .filter((part) => part.length > 0)
    .map((part) => {
      const run = { text: part, sourceOffset, timing: null, word: /^[\p{L}\p{N}]/u.test(part) };
      sourceOffset += [...part].length;
      return run;
    });
};
