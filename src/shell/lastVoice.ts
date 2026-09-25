// Which Voice a reader last chose for each Voice Model.
//
// The Engine holds the one chosen pair; this is the memory beside it, so
// stepping back to a Voice Model brings back the Voice the carousel was
// last left on rather than the Manifest's pick. It lives in the webview's
// own storage: nothing here leaves the machine, and losing it costs a few
// carousel steps, never a wrong Narration.

import type { CatalogEntry, CatalogVoice, VoiceSelection } from "../engine/client";

const KEY = "readily.lastVoice";

const read = (): Record<string, string> => {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed: unknown = raw === null ? {} : JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null
      ? (parsed as Record<string, string>)
      : {};
  } catch {
    return {};
  }
};

export const rememberVoice = ({ modelId, voiceId }: VoiceSelection) => {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...read(), [modelId]: voiceId }));
  } catch {
    // Storage refused: the Engine still has the choice.
  }
};

export const lastVoiceOf = (modelId: string): string | undefined => read()[modelId];

// The Voice to land on when a Voice Model is picked: the one the reader
// last left it on when that Voice is still offered, else the Manifest's
// pick, else whatever comes first. `undefined` only for a model with no
// Voices at all.
export const voiceToOffer = (entry: CatalogEntry): CatalogVoice | undefined => {
  const last = lastVoiceOf(entry.id);
  return (
    entry.voices.find((voice) => voice.id === last)
    ?? entry.voices.find((voice) => voice.id === entry.defaultVoiceId)
    ?? entry.voices[0]
  );
};
