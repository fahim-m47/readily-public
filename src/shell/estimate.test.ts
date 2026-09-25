import { expect, test } from "vitest";
import {
  SOURCE_CHARACTER_LIMIT,
  estimateSource,
  formatBytes,
  formatDuration,
} from "./estimate";

test("an empty Source has nothing to say", () => {
  expect(estimateSource("").summary).toBe("");
  expect(estimateSource("   \n ").summary).toBe("");
});

test("a Source is counted in characters and about-how-long", () => {
  expect(estimateSource("a".repeat(155)).summary).toBe("155 characters · about 0:10");
});

test("thousands are grouped so a long paste stays readable", () => {
  expect(estimateSource("a".repeat(1180)).summary).toBe(
    "1,180 characters · about 1:16",
  );
});

test("one character does not read as plural", () => {
  expect(estimateSource("a").summary).toBe("1 character · about 0:00");
});

test("a Source past the Engine's limit says how much to cut", () => {
  const estimate = estimateSource("a".repeat(SOURCE_CHARACTER_LIMIT + 84));

  expect(estimate.overBy).toBe(84);
  expect(estimate.summary).toBe(
    "1,000,084 characters · 84 more than Readily can narrate at once",
  );
});

test("a Source exactly at the limit is still narratable", () => {
  const estimate = estimateSource("a".repeat(SOURCE_CHARACTER_LIMIT));

  expect(estimate.overBy).toBe(0);
  expect(estimate.summary).toContain("about");
});

test("durations read as minutes and seconds, then hours and minutes", () => {
  expect(formatDuration(0)).toBe("0:00");
  expect(formatDuration(7.4)).toBe("0:07");
  expect(formatDuration(402)).toBe("6:42");
  expect(formatDuration(3600)).toBe("1h 0m");
  expect(formatDuration(5100)).toBe("1h 25m");
});

test("a negative duration never leaks a minus sign", () => {
  expect(formatDuration(-5)).toBe("0:00");
});

test("disk is a number a reader can check against Settings and the Finder", () => {
  expect(formatBytes(0)).toBe("0 bytes");
  expect(formatBytes(880)).toBe("880 bytes");
  expect(formatBytes(1024 * 512)).toBe("512 KB");
  expect(formatBytes(1024 * 1024 * 4.25)).toBe("4.3 MB");
  expect(formatBytes(1024 ** 3 * 2)).toBe("2.0 GB");
});
