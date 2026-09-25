import type { CatalogEntry, Mode } from "../engine/client";

// Simple mode offers only Voices whose current recipe has been qualified.
export function entriesForMode(entries: CatalogEntry[] | null, mode: Mode) {
  if (mode === "advanced" || entries === null) return entries;
  return entries
    .map((entry) => ({ ...entry, voices: entry.voices.filter((voice) => voice.simple) }))
    .filter((entry) => entry.voices.length > 0);
}
