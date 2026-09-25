import { expect, test } from "vitest";
import { loudness } from "./useAudition";

// The same numbers the Engine's `loudness()` test pins, so the carousel orb
// and the PlayerBar orb read one scale.
test("loudness maps RMS onto the Engine's decibel scale", () => {
  expect(loudness(0)).toBe(0);
  expect(loudness(-1)).toBe(0);
  expect(loudness(10 ** (-50 / 20))).toBeCloseTo(0, 6);
  expect(loudness(0.1)).toBeCloseTo(0.6, 6);
  expect(loudness(1)).toBe(1);
  expect(loudness(2)).toBe(1);
});
