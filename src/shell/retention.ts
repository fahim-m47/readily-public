// The two retention knobs, said the way a reader decides between them.
//
// ADR 0004 §5 defines a disk budget and a keep-audio-for-X-days window, both
// clocked by last-played, and evicted audio re-synthesizes silently on
// replay. So every budget label here is in hours of listening.

import type { DiskUsage } from "../engine/client";
import { formatBytes } from "./estimate";

// One option in a retention menu: what goes on the wire, and the words the
// reader picks it by.
export type Choice<T> = {
  value: T;
  label: string;
};

const GB = 1024 * 1024 * 1024;

// ADR 0004 §3: roughly 95 MB per narrated hour, FLAC at 24kHz mono. The same
// number that makes the ADR's own "5GB ≈ 50 narrated hours" true, kept here
// so the menu and the ADR cannot drift apart.
const FLAC_BYTES_PER_HOUR = 95 * 1024 * 1024;

// ADR 0015: off macOS, Segments are 24-bit WAV, counted at the 24kHz mono
// most Voice Models write, as the FLAC estimate is.
const WAV_BYTES_PER_HOUR = 24_000 * 3 * 60 * 60;

// Only a Mac's Engine writes FLAC. `main.tsx` tells the platforms apart the
// same way: WebKitGTK names Linux in its user agent.
const bytesPerHour = () =>
  navigator.userAgent.includes("Macintosh") ? FLAC_BYTES_PER_HOUR : WAV_BYTES_PER_HOUR;

// Sizes a reader might actually pick, around ADR 0004 §5's 5GB default.
const BUDGET_PRESETS = [1 * GB, 2 * GB, 5 * GB, 10 * GB, 20 * GB, 50 * GB];

// Windows short enough to be about tidiness and long enough to be about
// disk. `null` — Never — is the default and leads the list (ADR 0004 §5).
const KEEP_AUDIO_PRESETS = [7, 30, 90, 180];

// Hours as an estimate reads: round enough that nobody mistakes it for a
// measurement, and never rounded down to zero.
const roundHours = (hours: number) => {
  if (hours >= 30) return Math.round(hours / 10) * 10;
  if (hours >= 10) return Math.round(hours / 5) * 5;
  return Math.max(1, Math.round(hours));
};

const hoursIn = (bytes: number) => roundHours(bytes / bytesPerHour());

// The stored value always appears, even when it is not one of the presets:
// a `<select>` cannot show a value it has no option for, and the Engine
// accepts any budget in range.
const withCurrent = <T>(presets: readonly T[], current: T): T[] =>
  presets.includes(current) ? [...presets] : [...presets, current];

// The sizes the budget menu offers, smallest first, labelled by how much
// listening each holds.
export const budgetChoices = (current: number): Choice<number>[] =>
  withCurrent(BUDGET_PRESETS, current)
    .sort((a, b) => a - b)
    .map((bytes) => ({
      value: bytes,
      label: `${formatBytes(bytes)} · about ${hoursIn(bytes)} hours`,
    }));

// The windows the keep-audio menu offers: Never first, then the windows in
// order. Never is its own answer rather than a number of days, so it is never
// the value the preset list has to make room for.
export const keepAudioChoices = (current: number | null): Choice<number | null>[] => {
  const days =
    current === null ? [...KEEP_AUDIO_PRESETS] : withCurrent(KEEP_AUDIO_PRESETS, current);
  return [
    { value: null, label: "Never" },
    ...days.sort((a, b) => a - b).map((count) => ({ value: count, label: dayLabel(count) })),
  ];
};

const dayLabel = (days: number) =>
  days === 0 ? "After every play" : `${days} day${days === 1 ? "" : "s"}`;

// The one line that makes "deleting visibly frees disk" checkable: a total,
// then the two halves it is made of — the audio the budget governs, and the
// models the Catalog sheet's delete buttons govern (ADR 0003).
//
// "of models and audio", not "on disk": the same folder also holds the
// History database, the logs and the Python runtime, and the button under
// this line opens that folder in the Finder.
export const describeUsage = ({ modelsBytes, audioBytes }: DiskUsage) => {
  const audio =
    audioBytes === 0
      ? "no narrated audio yet"
      : `${formatBytes(audioBytes)} of narrated audio`;
  return `${formatBytes(modelsBytes + audioBytes)} of models and audio · ${formatBytes(modelsBytes)} of voice models, ${audio}`;
};

// What a settings write actually cost in audio — the bytes the Engine's own
// sweep counted as it deleted them (`evictedBytes`, docs/wire.md).
//
// Never work this out by subtracting two disk readings: the Engine sweeps on
// its own hourly schedule and a Narration synthesizing during a settings
// change adds audio the whole time, so the subtraction would misattribute
// both. `null` when the write took nothing, which is most writes.
export const describeEviction = (evictedBytes: number): string | null => {
  if (evictedBytes <= 0) return null;
  return `${formatBytes(evictedBytes)} of audio removed. That disk is free now.`;
};
