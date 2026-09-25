import { expect, test } from "vitest";
import type { RetentionSettings } from "../engine/client";
import {
  budgetChoices,
  describeEviction,
  describeUsage,
  keepAudioChoices,
} from "./retention";

const GB = 1024 * 1024 * 1024;
const MB = 1024 * 1024;

const DEFAULT_BUDGET = 5 * GB;

const retention = (over: Partial<RetentionSettings> = {}): RetentionSettings => ({
  segmentBudgetBytes: DEFAULT_BUDGET,
  keepAudioDays: null,
  diskUsage: { modelsBytes: 337 * MB, audioBytes: 900 * MB },
  ...over,
});

test("the budget menu offers sizes as hours of listening, not as bytes", () => {
  const labels = budgetChoices(DEFAULT_BUDGET).map((choice) => choice.label);
  expect(labels).toContain("5.0 GB · about 50 hours");
  expect(labels).toContain("1.0 GB · about 10 hours");
});

test("the default budget is one of the sizes the menu already offers", () => {
  const values = budgetChoices(DEFAULT_BUDGET).map((choice) => choice.value);
  expect(values).toContain(DEFAULT_BUDGET);
  expect(new Set(values).size).toBe(values.length);
});

test("a budget the menu does not offer joins it in size order, so the menu can show it", () => {
  const choices = budgetChoices(3 * GB);
  const values = choices.map((choice) => choice.value);
  expect(values).toContain(3 * GB);
  expect([...values].sort((a, b) => a - b)).toEqual(values);
  expect(choices.find((choice) => choice.value === 3 * GB)?.label).toBe(
    "3.0 GB · about 30 hours",
  );
});

test("keeping audio for a while is offered alongside never removing it on a clock", () => {
  const choices = keepAudioChoices(null);
  expect(choices[0]).toEqual({ value: null, label: "Never" });
  expect(choices.map((choice) => choice.label)).toContain("30 days");
});

test("a keep-audio setting the menu does not offer joins it in day order", () => {
  const choices = keepAudioChoices(14);
  const labels = choices.map((choice) => choice.label);
  expect(labels).toContain("14 days");
  expect(labels[0]).toBe("Never");
  const days = choices.slice(1).map((choice) => choice.value as number);
  expect([...days].sort((a, b) => a - b)).toEqual(days);
});

test("keeping audio for no days at all is offered as keeping none of it", () => {
  // `docs/wire.md` calls zero "keep nothing": every Narration's audio goes
  // at the next sweep, played or not.
  expect(keepAudioChoices(0).map((choice) => choice.label)).toContain("After every play");
});

test("disk usage names the two things taking the room", () => {
  expect(describeUsage(retention().diskUsage)).toBe(
    "1.2 GB of models and audio · 337.0 MB of voice models, 900.0 MB of narrated audio",
  );
});

test("disk usage says so when nothing has been narrated yet", () => {
  expect(describeUsage({ modelsBytes: 337 * MB, audioBytes: 0 })).toBe(
    "337.0 MB of models and audio · 337.0 MB of voice models, no narrated audio yet",
  );
});

test("audio removed to fit a lowered budget is reported as disk given back", () => {
  expect(describeEviction(500 * MB)).toBe(
    "500.0 MB of audio removed. That disk is free now.",
  );
});

test("a budget change that removed nothing is not reported as freeing disk", () => {
  expect(describeEviction(0)).toBe(null);
});
