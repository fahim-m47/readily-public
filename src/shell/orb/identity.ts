// Which orb a Voice gets: four colours and a seed, derived from where the
// Voice sits in its Voice Model's Catalog list rather than stored anywhere.
// Every call site reads the same unfiltered Catalog, so the carousel and
// the player show one orb for one Voice, and a Catalog that grows a Voice
// grows an orb for it with no curation step.
//
// The anchor hue walks round the wheel by the golden angle, one step per
// Voice, so any two Voices near each other in the carousel are far apart in
// hue. Sixty Voices cannot all be far apart on one wheel — the golden walk
// comes back within a few degrees of itself 8, 13, 21, 34 and 55 steps on
// — so the side colours cycle through nine leans, and none of those
// distances is a multiple of nine: Voices whose anchors nearly meet lean
// their palettes different ways, and Voices that lean alike are some
// forty-five degrees apart.

import type { CatalogEntry, VoiceSelection } from "../../engine/client";

export type OrbColors = readonly [string, string, string, string];
export type OrbIdentity = { colors: OrbColors; seed: number };

const GOLDEN_ANGLE = 137.508;
const LEANS = [22, 34, 46, 58, -22, -34, -46, -58, -70];

// FNV-1a over UTF-16 code units. Not for security; for a stable seed, and
// a stable start hue per Voice Model.
const fnv1a = (text: string) => {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash;
};

const hex = (channel: number) =>
  Math.round(channel * 255)
    .toString(16)
    .padStart(2, "0");

const hsl = (hue: number, saturation: number, lightness: number) => {
  const h = (((hue % 360) + 360) % 360) / 30;
  const a = saturation * Math.min(lightness, 1 - lightness);
  const channel = (n: number) => {
    const k = (n + h) % 12;
    return lightness - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  };
  return `#${hex(channel(0))}${hex(channel(8))}${hex(channel(4))}`;
};

// The shape every orb shares: a deep anchor, a lighter hue to one side of
// it, a pale tint from across the wheel, and a mid tone on the other side.
// `lean` is how far, and which way, the side colours sit from the anchor.
//
// Limes and yellows glare at a saturation that blues and corals wear well,
// so the anchor is muted through that band of the wheel.
const paletteAt = (hue: number, lean: number): OrbColors => {
  const turn = ((hue % 360) + 360) % 360;
  const glare = Math.max(0, 1 - Math.abs(turn - 100) / 60);
  return [
    hsl(hue, 0.72 - 0.3 * glare, 0.6 - 0.08 * glare),
    hsl(hue + lean, 0.6, 0.78),
    hsl(hue + 180, 1, 0.88),
    hsl(hue - lean * 1.2, 0.66, 0.72),
  ];
};

export const orbIdentity = (
  entries: readonly CatalogEntry[] | null,
  { modelId, voiceId }: VoiceSelection,
): OrbIdentity => {
  const hash = fnv1a(`${modelId}/${voiceId}`);
  const voices = entries?.find((entry) => entry.id === modelId)?.voices ?? [];
  const position = voices.findIndex((voice) => voice.id === voiceId);
  // A Voice the Catalog does not list — a Narration opened before the
  // Catalog has been read, say — still gets an orb, from its ids alone,
  // until the Catalog says where it sits.
  const step = position === -1 ? hash % 360 : position;
  const hue = (fnv1a(modelId) % 360) + step * GOLDEN_ANGLE;
  return {
    colors: paletteAt(hue, LEANS[step % LEANS.length]),
    seed: 1 + ((hash >>> 8) % 1000) / 100,
  };
};
