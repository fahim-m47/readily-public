import { expect, test } from "vitest";
import type { HistoryEntry } from "../engine/client";
import { describeRow, isReplayable, markGaps } from "./history";

const ENTRY: HistoryEntry = {
  id: "n-1",
  sourcePreview: "Read locally.",
  modelId: "kokoro:82m",
  voiceId: "af_heart",
  speed: 1,
  status: "finished",
  createdAt: "2026-08-26T09:00:00+00:00",
  updatedAt: "2026-08-26T09:00:00+00:00",
  lastPlayedAt: "2026-08-26T09:00:00+00:00",
  playheadSec: 0,
  totalDurationSec: 252,
  audioPresent: true,
  hasGaps: false,
};

// Dates are built in local time so the expectations hold in any timezone the
// suite runs in — the Engine sends UTC, the row shows the reader's own day.
const at = (
  year: number,
  month: number,
  day: number,
  hour = 9,
): string => new Date(year, month - 1, day, hour).toISOString();

const now = new Date(2026, 7, 26, 14, 0);

test("a row is the Source's first line, when it was made, and how long it runs", () => {
  const row = describeRow({ ...ENTRY, createdAt: at(2026, 8, 26) }, now);

  expect(row.title).toBe("Read locally.");
  expect(row.meta).toBe("Today · 4:12");
  expect(row.markers).toEqual([]);
});

test("a Source with no text of its own still gets a title", () => {
  const row = describeRow({ ...ENTRY, sourcePreview: "   \n  " }, now);

  expect(row.title).toBe("Untitled Narration");
});

test("a multi-line Source preview collapses to one line", () => {
  const row = describeRow(
    { ...ENTRY, sourcePreview: "  The Sea\nand the sky  " },
    now,
  );

  expect(row.title).toBe("The Sea and the sky");
});

test("a Narration that never finished says when it was made and nothing more", () => {
  const row = describeRow(
    {
      ...ENTRY,
      status: "interrupted",
      createdAt: at(2026, 8, 25),
      totalDurationSec: null,
    },
    now,
  );

  expect(row.meta).toBe("Yesterday");
});

test("older Narrations are dated, and last year's carry the year", () => {
  expect(describeRow({ ...ENTRY, createdAt: at(2026, 8, 2) }, now).meta).toBe(
    "Aug 2 · 4:12",
  );
  expect(describeRow({ ...ENTRY, createdAt: at(2025, 12, 30) }, now).meta).toBe(
    "Dec 30, 2025 · 4:12",
  );
});

test("evicted audio and skipped Blocks are the two markers a row can carry", () => {
  const row = describeRow(
    { ...ENTRY, audioPresent: false, hasGaps: true },
    now,
  );

  expect(row.markers.map((marker) => marker.kind)).toEqual(["evicted", "gaps"]);
  expect(row.markers[0].label).toMatch(/re-made/);
  expect(row.markers[1].label).toMatch(/skipped/);
});

test("only a Narration the Engine would take again is replayed on selection", () => {
  expect(isReplayable("finished")).toBe(true);
  expect(isReplayable("interrupted")).toBe(true);
  expect(isReplayable("stopped")).toBe(true);
  // Active Narrations and failed ones are `409 narration_not_resumable`.
  expect(isReplayable("playing")).toBe(false);
  expect(isReplayable("preparing")).toBe(false);
  expect(isReplayable("failed")).toBe(false);
});

test("gaps cut the Source into read and skipped runs", () => {
  const pieces = markGaps("One. Two. Three.", [
    { ordinal: 1, sourceStart: 5, sourceEnd: 9, errorCode: "block_failed", createdAt: "" },
  ]);

  expect(pieces).toEqual([
    { text: "One. ", skipped: false },
    { text: "Two.", skipped: true },
    { text: " Three.", skipped: false },
  ]);
});

test("a Source with no gaps is one unbroken run", () => {
  expect(markGaps("One. Two.", [])).toEqual([
    { text: "One. Two.", skipped: false },
  ]);
});

test("gaps are marked in Source order however the Engine listed them", () => {
  const pieces = markGaps("abcdefgh", [
    { ordinal: 3, sourceStart: 6, sourceEnd: 8, errorCode: "x", createdAt: "" },
    { ordinal: 1, sourceStart: 0, sourceEnd: 2, errorCode: "x", createdAt: "" },
  ]);

  expect(pieces.map((piece) => piece.text)).toEqual(["ab", "cdef", "gh"]);
  expect(pieces.map((piece) => piece.skipped)).toEqual([true, false, true]);
});

test("a gap that overruns the Source it indexes cannot lose a character", () => {
  const source = "abcdef";
  const pieces = markGaps(source, [
    { ordinal: 1, sourceStart: 4, sourceEnd: 99, errorCode: "x", createdAt: "" },
    { ordinal: 2, sourceStart: 2, sourceEnd: 5, errorCode: "x", createdAt: "" },
    { ordinal: 3, sourceStart: -3, sourceEnd: 1, errorCode: "x", createdAt: "" },
  ]);

  expect(pieces.map((piece) => piece.text).join("")).toBe(source);
  expect(pieces).toEqual([
    { text: "a", skipped: true },
    { text: "b", skipped: false },
    { text: "cdef", skipped: true },
  ]);
});

test("an emoji before a gap does not slide the mark onto the wrong characters", () => {
  // Gap ranges are Python string indices — code points — and the owl is one
  // code point but two UTF-16 code units.
  const pieces = markGaps("🦉 sings. Loudly.", [
    { ordinal: 1, sourceStart: 2, sourceEnd: 8, errorCode: "x", createdAt: "" },
  ]);

  expect(pieces).toEqual([
    { text: "🦉 ", skipped: false },
    { text: "sings.", skipped: true },
    { text: " Loudly.", skipped: false },
  ]);
});
