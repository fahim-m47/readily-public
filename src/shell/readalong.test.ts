import { expect, test } from "vitest";
import type { HistoryNarration, HistorySegment } from "../engine/client";
import {
  activeBlockIndex,
  formatPlayhead,
  readAlongBlocks,
  readPieces,
} from "./readalong";
import wordFixture from "../../engine/tests/fixtures/words.json";

const block = (
  ordinal: number,
  sourceStart: number,
  sourceEnd: number,
  timelineStartSec: number | null,
): HistorySegment => ({
  ordinal,
  sourceStart,
  sourceEnd,
  boundary: "paragraph",
  durationSec: 1,
  audioPresent: true,
  timelineStartSec,
  timings: timelineStartSec === null
    ? []
    : [{ sourceStart, sourceEnd: sourceStart + 3, startSec: timelineStartSec, endSec: timelineStartSec + 0.5, provenance: "spoken" }],
});

const document = (
  source: string,
  segments: HistorySegment[],
  gaps: HistoryNarration["gaps"] = [],
) => ({ source, segments, gaps });

const textOf = (pieces: { text: string }[]) =>
  pieces.map((piece) => piece.text).join("");

test("the rendered Blocks put the Source back together exactly as it arrived", () => {
  const source = "One.\n\nTwo.\n\nThree.";
  const blocks = readAlongBlocks(
    document(source, [block(0, 0, 4, 0), block(1, 6, 10, 1.2), block(2, 12, 18, 2.4)]),
  );

  expect(blocks.map((piece) => textOf(piece.pieces)).join("")).toBe(source);
  expect(blocks).toHaveLength(3);
});

test("Source the Engine has not chunked yet renders, and is not a seek target", () => {
  const source = "One. Two. Three.";
  const blocks = readAlongBlocks(document(source, [block(0, 0, 4, 0)]));

  expect(blocks).toHaveLength(2);
  expect(blocks[1].ordinal).toBe(null);
  expect(blocks[1].timeline).toBe(null);
  expect(textOf(blocks[1].pieces)).toBe(" Two. Three.");
});

test("a Block the Engine has not reached carries no offset, so it cannot be seeked to", () => {
  const blocks = readAlongBlocks(
    document("One. Two.", [block(0, 0, 4, 0), block(1, 5, 9, null)]),
  );

  expect(blocks[1].timeline).toBe(null);
});

test("overlapping and out-of-order Segment ranges still spend every character once", () => {
  const source = "abcdef";
  const blocks = readAlongBlocks(
    document(source, [
      block(2, 4, 99, 2),
      block(0, -5, 3, 0),
      block(1, 1, 2, 1),
    ]),
  );

  expect(blocks.map((piece) => textOf(piece.pieces)).join("")).toBe(source);
  expect(blocks.map((piece) => piece.ordinal)).toEqual([0, 2]);
});

test("a gap marks the words it cost inside the Block that lost them", () => {
  const blocks = readAlongBlocks(
    document(
      "One. Two. Three.",
      [block(0, 0, 4, 0), block(1, 5, 9, 1), block(2, 10, 16, 2)],
      [
        {
          ordinal: 1,
          sourceStart: 5,
          sourceEnd: 9,
          errorCode: "synthesis_failed",
          createdAt: "2026-01-01T00:00:00Z",
        },
      ],
    ),
  );

  expect(blocks.map((piece) => piece.skipped)).toEqual([false, true, false]);
  expect(textOf(blocks[1].pieces.filter((piece) => piece.skipped))).toBe("Two.");
});

test("the highlight lands on the last Block that has started, not the nearest one", () => {
  const blocks = readAlongBlocks(
    document("One. Two. Three.", [
      block(0, 0, 4, 0),
      block(1, 5, 9, 4),
      block(2, 10, 16, 9),
    ]),
  );

  expect(activeBlockIndex(blocks, 0)).toBe(0);
  expect(activeBlockIndex(blocks, 3.9)).toBe(0);
  expect(activeBlockIndex(blocks, 4)).toBe(1);
  expect(activeBlockIndex(blocks, 900)).toBe(2);
});

test("nothing is highlighted before the Engine has stamped a single offset", () => {
  const blocks = readAlongBlocks(document("One. Two.", [block(0, 0, 9, null)]));

  expect(activeBlockIndex(blocks, 0)).toBe(-1);
});

test("the clock never reads past what has been assembled", () => {
  expect(formatPlayhead(0, 62)).toEqual(["0:00", "1:02"]);
  expect(formatPlayhead(30.4, 62)).toEqual(["0:30", "1:02"]);
  // A playhead snapshot can arrive a beat ahead of the total it belongs to.
  expect(formatPlayhead(70, 62)).toEqual(["1:02", "1:02"]);
  expect(formatPlayhead(-1, 0)).toEqual(["0:00", "0:00"]);
});

test("an emoji does not shift every Block boundary that follows it", () => {
  // The Engine's ranges are Python string indices — code points — and the
  // owl is one code point but two UTF-16 code units.
  const source = "🦉 hoots. Twice.";
  const blocks = readAlongBlocks(
    document(source, [block(0, 0, 8, 0), block(1, 9, 15, 2)]),
  );

  expect(textOf(blocks[0].pieces)).toBe("🦉 hoots.");
  expect(textOf(blocks[1].pieces)).toBe(" Twice.");
  expect(blocks[1].timeline?.sourceOffset).toBe(9);
  expect(blocks).toHaveLength(2);
});

test("a cut given the previous one hands back every Block whose inputs did not change", () => {
  const source = "One. Two. Three. Four. Five.";
  const settled = (ordinal: number, sourceStart: number, sourceEnd: number) =>
    block(ordinal, sourceStart, sourceEnd, ordinal);
  const pending = (ordinal: number, sourceStart: number, sourceEnd: number) => ({
    ...block(ordinal, sourceStart, sourceEnd, null), durationSec: null, audioPresent: false,
  });
  const gap = { ordinal: 0, sourceStart: 0, sourceEnd: 4, errorCode: "synthesis_failed", createdAt: "2026-01-01T00:00:00Z" };
  const one = settled(0, 0, 4);
  const two = settled(1, 5, 9);
  const rest = [pending(3, 17, 22), pending(4, 23, 28)];
  const before = readAlongBlocks(document(source, [one, two, pending(2, 10, 16), ...rest]));

  const fresh = <T extends HistorySegment>(segment: T) => structuredClone(segment);
  const after = readAlongBlocks(
    document(source, [one, two, settled(2, 10, 16), ...rest].map(fresh), []),
    before,
  );
  expect(after[0]).toBe(before[0]);
  expect(after[1]).toBe(before[1]);
  expect(after[2]).not.toBe(before[2]);
  expect(after[2].timeline).toEqual({ sourceOffset: 10, startSec: 2 });
  expect(after[3]).not.toBe(before[3]);
  expect(after[3].seekSourceOffset).toBe(17);
  expect(after[4]).toBe(before[4]);
  expect(after.map((piece) => textOf(piece.pieces)).join("")).toBe(source);

  const gapped = readAlongBlocks(document(source, [one, two, settled(2, 10, 16), ...rest], [gap]), after);
  expect(gapped[0]).not.toBe(after[0]);
  expect(gapped[0].skipped).toBe(true);
  gapped.slice(1).forEach((piece, index) => expect(piece).toBe(after[index + 1]));

  const otherSource = readAlongBlocks(document(`${source} `, [one, two, settled(2, 10, 16), ...rest]), after);
  otherSource.forEach((piece, index) => expect(piece).not.toBe(after[index]));
});

test("readPieces covers every character once, in Source order, around timed words and gaps", () => {
  const block = readAlongBlocks({
    source: "🌊 Don’t stop. 42 skipped end",
    segments: [{
      ordinal: 0, sourceStart: 0, sourceEnd: 28, boundary: "paragraph", durationSec: 2,
      audioPresent: true, timelineStartSec: 0,
      timings: [{ sourceStart: 2, sourceEnd: 7, startSec: 0, endSec: 0.5, provenance: "spoken" }],
    }],
    gaps: [{ ordinal: 0, sourceStart: 17, sourceEnd: 24, errorCode: "generation_failed", createdAt: "2026-09-06" }],
  })[0];
  const pieces = readPieces(block);
  expect(pieces.map((piece) => piece.text).join("")).toBe("🌊 Don’t stop. 42 skipped end");
  expect(pieces.map((piece) => [piece.skipped, piece.sourceOffset, piece.runs.length])).toEqual([
    [false, 0, 7], [true, 17, 0], [false, 24, 2],
  ]);
  const runs = pieces.flatMap((piece) => piece.runs);
  expect(runs.map((run) => [run.word, run.sourceOffset])).toEqual([
    [false, 0], [true, 2], [false, 7], [true, 8], [false, 12], [true, 14], [false, 16],
    [false, 24], [true, 25],
  ]);
  expect(runs[1].timing?.provenance).toBe("spoken");
  expect(runs.filter((run) => run.timing === null && run.word).map((run) => run.text)).toEqual(["stop", "42", "end"]);
});

test("the shell cuts the same words out of the Source as the Engine", () => {
  const text = wordFixture.text;
  const block = readAlongBlocks({
    source: text,
    segments: [{
      ordinal: 0, sourceStart: 0, sourceEnd: [...text].length, boundary: "paragraph", durationSec: 2,
      audioPresent: true, timelineStartSec: 0, timings: [],
    }],
    gaps: [],
  })[0];
  const words = readPieces(block)
    .flatMap((piece) => piece.runs)
    .filter((run) => run.word)
    .map((run) => [run.sourceOffset, run.sourceOffset + [...run.text].length]);
  expect(words).toEqual(wordFixture.words);
});
