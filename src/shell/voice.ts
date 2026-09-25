// What the composer's Voice pill says, and which Voices it can offer.
//
// Tier is the Manifest's own word, carried through untouched (CONTEXT.md) —
// the shell has no second vocabulary for how fast a Voice Model is.

import type { CatalogEntry, CatalogVoice, ModelStatus, VoiceSelection } from "../engine/client";

// One Voice Model a reader can pick from right now: on disk, with the
// Voices it offers.
export type InstalledEntry = {
  entry: CatalogEntry;
  voices: readonly CatalogVoice[];
};

// The Voice Models the popover lists, in the Catalog's own order.
//
// Installed only, because the popover is the one-click path: acquiring a
// Voice Model is the deliberate detour behind "Browse all voice models…".
// An entry the store has not answered for yet is left out.
export const installedEntries = (
  entries: readonly CatalogEntry[] | null,
  statusOf: (entryId: string) => ModelStatus | undefined,
): InstalledEntry[] =>
  (entries ?? [])
    .filter((entry) => statusOf(entry.id)?.installed === true)
    .map((entry) => ({ entry, voices: entry.voices }));

// The pill's own label — "Kokoro · Heart" — or `null` while the pair names
// nothing the Catalog knows about.
//
// `null` is a transient: the Engine resolves the selection against the
// baked Manifest before answering, so an unnameable pair means the Catalog
// has not been read yet, not that the Voice is gone.
export const describeSelection = (
  entries: readonly CatalogEntry[] | null,
  selection: VoiceSelection | null,
): string | null => {
  if (selection === null || entries === null) return null;
  const entry = entries.find((candidate) => candidate.id === selection.modelId);
  const voice = entry?.voices.find((candidate) => candidate.id === selection.voiceId);
  if (!entry || !voice) return null;
  return `${entry.name} · ${voice.name}`;
};

// Whether this Voice is the chosen one. A pair, never a model alone: two
// Voices of the same Voice Model are two different choices.
export const isSelected = (
  selection: VoiceSelection | null,
  entryId: string,
  voiceId: string,
) => selection?.modelId === entryId && selection.voiceId === voiceId;
