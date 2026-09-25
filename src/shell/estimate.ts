// What a thing costs, said the way a reader checks it: characters, minutes,
// megabytes.

// The Engine's own cap on one Narration's Source (`input`, `max_length` on
// the v1 speech route). A million characters is a few thick novels — the
// same order as ElevenReader's 500-page upload bound — so it bounds a
// request body, not a reader. Enforced here so an over-long paste is a
// sentence the reader can act on rather than a 422 arriving after they
// press Narrate.
export const SOURCE_CHARACTER_LIMIT = 1_000_000;

// Roughly 185 words a minute: a plausible listening pace, not a measurement,
// so the word "about" in the rendered string is doing real work. The true
// duration is the Voice Model's to decide and is not known until the
// Narration has been synthesized.
const CHARACTERS_PER_SECOND = 15.5;

// Seconds as a listener reads a clock: `0:07`, `6:42`, and `1h 25m` once
// the seconds have stopped being interesting.
export const formatDuration = (seconds: number) => {
  const whole = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(whole / 60);
  if (minutes >= 60) return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  return `${minutes}:${String(whole % 60).padStart(2, "0")}`;
};

export type SourceEstimate = {
  characters: number;
  // Characters past `SOURCE_CHARACTER_LIMIT`; `0` when the Source fits.
  overBy: number;
  // Whether the Source is something the Engine would accept: it has text,
  // and it fits. The composer's Narrate button is this, plus a ready Engine.
  narratable: boolean;
  // The composer's one line about the Source. Empty when there is no text —
  // an empty composer says nothing rather than saying "0 characters".
  summary: string;
};

const count = (characters: number) =>
  `${characters.toLocaleString("en-US")} character${characters === 1 ? "" : "s"}`;

// Reads a composer's raw text and says what the composer should show.
// Whitespace-only text counts as no text at all — the Engine rejects a
// blank Source, so the shell does not offer to send one.
//
// Counted in code points, because the Engine's cap is Python's `len()`.
export const estimateSource = (text: string): SourceEstimate => {
  const characters = [...text].length;
  const overBy = Math.max(0, characters - SOURCE_CHARACTER_LIMIT);
  const empty = text.trim().length === 0;

  const summary = empty
    ? ""
    : overBy > 0
      ? `${count(characters)} · ${overBy.toLocaleString("en-US")} more than Readily can narrate at once`
      : `${count(characters)} · about ${formatDuration(characters / CHARACTERS_PER_SECOND)}`;

  return { characters, overBy, narratable: !empty && overBy === 0, summary };
};

// Disk as a reader checks it, against the Finder. Binary units, because the
// retention budget is one (ADR 0004 §5).
export const formatBytes = (bytes: number) => {
  const safe = Math.max(0, bytes);
  if (safe < 1024) return `${Math.round(safe)} bytes`;
  const kilobytes = safe / 1024;
  if (kilobytes < 1024) return `${Math.round(kilobytes)} KB`;
  const megabytes = kilobytes / 1024;
  if (megabytes < 1024) return `${megabytes.toFixed(1)} MB`;
  return `${(megabytes / 1024).toFixed(1)} GB`;
};
