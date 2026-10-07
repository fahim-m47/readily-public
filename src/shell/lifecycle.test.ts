import { expect, test } from "vitest";
import type { Diagnostics } from "../engine/advanced";
import type { NarrationState } from "../engine/client";
import { describeConnection, describeNarration, isGenerating, isReading, isStoppable } from "./lifecycle";

const QUIET: Diagnostics = {
  audioSecondsPerSecond: null,
  readySecondsAhead: 0,
  preparingBlock: null,
  generationComplete: false,
  playingBlock: null,
  retries: 0,
  cutoffs: 0,
  ringStarvations: 0,
  deviceUnderflows: 0,
};

const IDLE: NarrationState = {
  version: 1,
  phase: "idle",
  narrationId: null,
  modelId: "kokoro:82m",
  voiceId: "af_heart",
  positionSec: 0,
  totalSec: 0,
  speed: 1,
  generationBehind: false,
  diagnostics: QUIET,
  level: 0,
  lateCallbacks: 0,
  error: null,
};

test("the Engine's boot beats are named, not numbered", () => {
  expect(describeConnection({ state: "starting", detail: "provisioning", note: null })).toEqual({
    tone: "working",
    message: "Setting up the Engine…",
  });
  expect(describeConnection({ state: "starting", detail: "launching" })).toEqual({
    tone: "working",
    message: "Starting the Engine…",
  });
  expect(describeConnection({ state: "starting", detail: "restarting" })).toEqual({
    tone: "working",
    message: "Restarting the Engine…",
  });
  expect(describeConnection({ state: "ready" })).toEqual({
    tone: "ready",
    message: "Engine ready",
  });
});

test("a failed Engine says the supervisor's reason", () => {
  expect(
    describeConnection({ state: "failed", message: "The Engine would not start." }),
  ).toEqual({ tone: "failed", message: "The Engine would not start." });
});

test("an idle Engine has no Narration line to show", () => {
  expect(describeNarration(null)).toBe(null);
  expect(describeNarration(IDLE)).toBe(null);
});

test("preparing gets one sentence, because the Engine reports one phase", () => {
  expect(describeNarration({ ...IDLE, phase: "preparing" })).toEqual({
    tone: "working",
    message: "Getting the words ready…",
  });
});

test("every phase the wire can send reads as a plain sentence", () => {
  expect(describeNarration({ ...IDLE, phase: "playing" })).toEqual({
    tone: "working",
    message: "Reading aloud…",
  });
  expect(describeNarration({ ...IDLE, phase: "paused" })).toEqual({
    tone: "ready",
    message: "Paused.",
  });
  expect(describeNarration({ ...IDLE, phase: "finished" })).toEqual({
    tone: "ready",
    message: "Finished reading.",
  });
});

test("Stop is offered in exactly the phases the Engine can stop", () => {
  expect(isStoppable(null)).toBe(false);
  expect(isStoppable(IDLE)).toBe(false);
  expect(isStoppable({ ...IDLE, phase: "preparing" })).toBe(true);
  expect(isStoppable({ ...IDLE, phase: "playing" })).toBe(true);
  expect(isStoppable({ ...IDLE, phase: "paused" })).toBe(true);
  expect(isStoppable({ ...IDLE, phase: "finished" })).toBe(false);
  expect(isStoppable({ ...IDLE, phase: "failed" })).toBe(false);
});

test("a paused Narration can be stopped but is not being read", () => {
  expect(isReading({ ...IDLE, phase: "preparing" })).toBe(true);
  expect(isReading({ ...IDLE, phase: "playing" })).toBe(true);
  expect(isReading({ ...IDLE, phase: "paused" })).toBe(false);
  expect(isReading({ ...IDLE, phase: "finished" })).toBe(false);
  expect(isReading(null)).toBe(false);
});

test("moving on asks about generation left to do, never about finished audio playing out", () => {
  const unfinished: Diagnostics = { ...QUIET, generationComplete: false };
  const finished: Diagnostics = { ...QUIET, generationComplete: true };
  expect(isGenerating({ ...IDLE, phase: "preparing", diagnostics: unfinished })).toBe(true);
  expect(isGenerating({ ...IDLE, phase: "playing", diagnostics: unfinished })).toBe(true);
  expect(isGenerating({ ...IDLE, phase: "playing", diagnostics: finished })).toBe(false);
  expect(isGenerating({ ...IDLE, phase: "paused", diagnostics: unfinished })).toBe(false);
  expect(isGenerating({ ...IDLE, phase: "finished", diagnostics: finished })).toBe(false);
  expect(isGenerating(null)).toBe(false);
});

test("a failed Narration shows the Engine's own sentence", () => {
  const failed: NarrationState = {
    ...IDLE,
    phase: "failed",
    error: {
      version: 1,
      code: "generation_failed",
      message: "The voice could not read this text.",
    },
  };

  expect(describeNarration(failed)).toEqual({
    tone: "failed",
    message: "The voice could not read this text.",
  });
});

test("a failed Narration with no error still says something", () => {
  expect(describeNarration({ ...IDLE, phase: "failed" })).toEqual({
    tone: "failed",
    message: "The Narration failed.",
  });
});

test("finishing the remaining Blocks keeps the gap report visible", () => {
  expect(describeNarration({
    ...IDLE,
    phase: "finished",
    error: { version: 1, code: "generation_gap", message: "Some words could not be read." },
  })).toEqual({ tone: "failed", message: "Some words could not be read." });
});
