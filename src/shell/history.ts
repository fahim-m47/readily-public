// What a History row says, in plain language.
//
// A row carries two markers and no others — audio retention swept away
// (ADR 0004 §5), and Blocks the Engine could not read (ADR 0002 §8) —
// because both change what happens when the row is selected.

import type { HistoryEntry, HistoryGap, NarrationStatusName } from "../engine/client";
import { formatDuration } from "./estimate";
import { sourceText } from "./source";

// `kind` selects the presentation; `label` carries the accessible meaning.
export type RowMarker = {
  kind: "evicted" | "gaps";
  label: string;
};

export type HistoryRow = {
  title: string;
  // When it was made, and how long it runs once that is known.
  meta: string;
  markers: RowMarker[];
};

const DAY_MS = 24 * 60 * 60 * 1000;

const startOfDay = (date: Date) =>
  new Date(date.getFullYear(), date.getMonth(), date.getDate());

// The reader's own calendar, not the Engine's: the wire sends UTC, and a
// Narration made at 11pm belongs to the day the reader made it. Counted in
// whole days so a 23- or 25-hour daylight-saving night still reads as
// "Yesterday".
const formatWhen = (iso: string, now: Date) => {
  const made = new Date(iso);
  if (Number.isNaN(made.getTime())) return "";

  const days = Math.round(
    (startOfDay(now).getTime() - startOfDay(made).getTime()) / DAY_MS,
  );
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";

  return made.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(made.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
};

const markersFor = (entry: HistoryEntry): RowMarker[] => {
  const markers: RowMarker[] = [];
  if (!entry.audioPresent) {
    markers.push({
      kind: "evicted",
      label: "Audio cleaned up to save disk — it is re-made when you play this.",
    });
  }
  if (entry.hasGaps) {
    markers.push({
      kind: "gaps",
      label: "Some text could not be read and was skipped.",
    });
  }
  return markers;
};

// The Source's first words as a title, with its line breaks folded away.
// A Source of nothing but whitespace gets a name rather than a blank.
export const titleOf = (entry: HistoryEntry) =>
  entry.sourcePreview.replace(/\s+/g, " ").trim() || "Untitled Narration";

// Turns one History summary into the three things its row shows.
export const describeRow = (
  entry: HistoryEntry,
  now: Date = new Date(),
): HistoryRow => {
  const duration =
    entry.totalDurationSec === null ? "" : formatDuration(entry.totalDurationSec);

  return {
    title: titleOf(entry),
    meta: [formatWhen(entry.createdAt, now), duration].filter(Boolean).join(" · "),
    markers: markersFor(entry),
  };
};

// Whether selecting this row should ask the Engine to play it. Resume takes
// an `interrupted` or `stopped` Narration at its playhead and replays a
// `finished` one from the start; anything else answers
// `409 narration_not_resumable`, so the shell does not ask.
export const isReplayable = (status: NarrationStatusName) =>
  status === "interrupted" || status === "stopped" || status === "finished";

export type SourcePiece = { text: string; skipped: boolean };

// Cuts a Narration's Source into the runs that were read and the runs that
// were skipped, so the gaps on its row can be pointed at in the text.
//
// The ranges are the Engine's, so they are clamped, ordered and merged here
// rather than trusted: "silent text loss is the one forbidden outcome"
// (ADR 0002 §8), so every character is emitted exactly once.
export const markGaps = (
  source: string,
  gaps: readonly HistoryGap[],
): SourcePiece[] => {
  const text = sourceText(source);
  const merged: { start: number; end: number }[] = [];
  const ranges = gaps
    .map((gap) => ({
      start: Math.min(Math.max(gap.sourceStart, 0), text.length),
      end: Math.min(Math.max(gap.sourceEnd, 0), text.length),
    }))
    .filter((range) => range.end > range.start)
    .sort((left, right) => left.start - right.start);

  for (const range of ranges) {
    const last = merged.at(-1);
    if (last && range.start <= last.end) last.end = Math.max(last.end, range.end);
    else merged.push({ ...range });
  }

  const pieces: SourcePiece[] = [];
  let cursor = 0;
  for (const range of merged) {
    if (range.start > cursor) {
      pieces.push({ text: text.slice(cursor, range.start), skipped: false });
    }
    pieces.push({ text: text.slice(range.start, range.end), skipped: true });
    cursor = range.end;
  }
  if (cursor < text.length) {
    pieces.push({ text: text.slice(cursor), skipped: false });
  }

  return pieces.length > 0 ? pieces : [{ text: source, skipped: false }];
};
