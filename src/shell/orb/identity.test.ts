import { expect, test } from "vitest";
import type { CatalogEntry } from "../../engine/client";
import { orbIdentity } from "./identity";

const voices = Array.from({ length: 60 }, (_, at) => ({ id: `voice-${at}` }));
const entries = [{ id: "kokoro", voices }, { id: "qwen", voices }] as CatalogEntry[];
const orb = (voiceId: string, modelId = "kokoro") => orbIdentity(entries, { modelId, voiceId });

// Hue of a colour, read back from its hex.
const hueOf = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map((at) => parseInt(hex.slice(at, at + 2), 16) / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return (h * 60 + 360) % 360;
};
const apart = (a: number, b: number) => Math.min(Math.abs(a - b), 360 - Math.abs(a - b));
// How far round, and which way, `b` sits from `a`.
const lean = (a: number, b: number) => ((((b - a) % 360) + 540) % 360) - 180;

test("the same Voice always gets the same orb", () => {
  expect(orb("voice-7")).toEqual(orb("voice-7"));
});

test("neighbours in the carousel are far apart in hue", () => {
  for (let at = 0; at + 2 < voices.length; at += 1) {
    const here = hueOf(orb(voices[at].id).colors[0]);
    for (const other of [voices[at + 1], voices[at + 2]]) {
      expect(apart(here, hueOf(orb(other.id).colors[0]))).toBeGreaterThan(60);
    }
  }
});

test("no two Voices of sixty share both their anchor and their lean", () => {
  const shapes = voices.map((voice) => {
    const [anchor, beside] = orb(voice.id).colors;
    return { anchor: hueOf(anchor), lean: lean(hueOf(anchor), hueOf(beside)) };
  });
  for (const [at, here] of shapes.entries()) {
    for (const there of shapes.slice(at + 1)) {
      const alike = apart(here.anchor, there.anchor) < 30 && Math.abs(here.lean - there.lean) < 10;
      expect(alike, JSON.stringify([here, there])).toBe(false);
    }
  }
});

test("two Voice Models start their walks from different hues", () => {
  expect(orb("voice-0", "kokoro").colors).not.toEqual(orb("voice-0", "qwen").colors);
});

test("a Voice the Catalog does not list still gets an orb from its ids", () => {
  const orphan = orbIdentity(null, { modelId: "kokoro", voiceId: "af_heart" });
  expect(orphan).toEqual(orbIdentity([], { modelId: "kokoro", voiceId: "af_heart" }));
  expect(orphan.colors).not.toEqual(orbIdentity(null, { modelId: "kokoro", voiceId: "am_adam" }).colors);
});
