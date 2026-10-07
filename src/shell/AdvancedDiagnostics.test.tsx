import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { BlockDiagnostics, Diagnostics } from "../engine/advanced";
import type { EngineClient, NarrationState } from "../engine/client";
import AdvancedDiagnostics from "./AdvancedDiagnostics";

afterEach(cleanup);

const BLOCK: BlockDiagnostics = {
  ordinal: 2,
  recordHash: "a".repeat(64),
  seed: 90210,
  cacheHit: false,
  wordTiming: "matched",
  supportModel: "whisperx:tiny",
  take: "B",
  hasComparison: true,
};

const REPORT: Diagnostics = {
  audioSecondsPerSecond: null,
  readySecondsAhead: 3.42,
  preparingBlock: 4,
  generationComplete: false,
  playingBlock: BLOCK,
  retries: 1,
  cutoffs: 2,
  ringStarvations: 3,
  deviceUnderflows: 4,
};

const reading = (diagnostics: Diagnostics): NarrationState => ({
  version: 1,
  phase: "playing",
  narrationId: "n-1",
  modelId: "kokoro:82m",
  voiceId: "af_heart",
  positionSec: 0,
  totalSec: 0,
  speed: 2,
  generationBehind: false,
  diagnostics,
  level: 0,
  lateCallbacks: 0,
  error: null,
});

const mount = (
  narration: NarrationState | null,
  selectTake = vi.fn<EngineClient["selectTake"]>(async () => {}),
) => {
  const onTakeSelected = vi.fn();
  render(
    <AdvancedDiagnostics
      client={{ selectTake }}
      narration={narration}
      supportModels={[{ id: "whisperx:tiny", name: "WhisperX tiny" }]}
      onTakeSelected={onTakeSelected}
    />,
  );
  return { selectTake, onTakeSelected };
};

const metric = (term: string) =>
  screen.getByText(term).parentElement?.querySelector("dd")?.textContent;

test("throughput nobody has measured yet says so rather than showing a zero", () => {
  mount(reading(REPORT));
  expect(metric("Generation throughput")).toBe("Waiting for generation");
  expect(metric("Ready ahead")).toBe("3.4 seconds at 2×");
  expect(metric("Preparing")).toBe("Block 5");
  expect(metric("Retries / cutoffs")).toBe("1 / 2");
  expect(metric("Ring starvation callbacks")).toBe("3");
  expect(metric("Device underflows")).toBe("4");
});

test("a measured throughput is reported in the Engine's own unit", () => {
  mount(reading({ ...REPORT, audioSecondsPerSecond: 1.5, preparingBlock: null }));
  expect(metric("Generation throughput")).toBe("1.50 seconds audio / second");
  expect(metric("Preparing")).toBe("Nothing");
});

test("nothing is measured before there is a Narration", () => {
  mount(null);
  expect(screen.getByText("Start a Narration to see measurements.")).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
});

test("the Block section names the take, its Support Model and its Generation Record", () => {
  mount(reading(REPORT));
  expect(screen.getByRole("heading", { name: "Block 3 · Take B" })).toBeTruthy();
  expect(screen.getByText("Word timing: matched · WhisperX tiny")).toBeTruthy();
  expect(screen.getByText("Newly generated Segment · Seed 90210")).toBeTruthy();
  expect(screen.getByText("a".repeat(64))).toBeTruthy();
});

test("a stored Segment is named as one", () => {
  mount(reading({ ...REPORT, playingBlock: { ...BLOCK, cacheHit: true, supportModel: null } }));
  expect(screen.getByText("Stored Segment · Seed 90210")).toBeTruthy();
  expect(screen.getByText("Word timing: matched")).toBeTruthy();
});

test("each take button asks for that take of this Block, then re-reads the document", async () => {
  const { selectTake, onTakeSelected } = mount(reading(REPORT));

  fireEvent.click(screen.getByRole("button", { name: "Regenerate this Block" }));
  await waitFor(() => expect(onTakeSelected).toHaveBeenCalledOnce());
  expect(selectTake).toHaveBeenCalledWith("n-1", 2, "reroll");

  fireEvent.click(screen.getByRole("button", { name: "Play A · stored take" }));
  await waitFor(() => expect(onTakeSelected).toHaveBeenCalledTimes(2));
  expect(selectTake).toHaveBeenLastCalledWith("n-1", 2, "A");

  fireEvent.click(screen.getByRole("button", { name: "Play B · re-roll" }));
  await waitFor(() => expect(onTakeSelected).toHaveBeenCalledTimes(3));
  expect(selectTake).toHaveBeenLastCalledWith("n-1", 2, "B");

  expect(selectTake.mock.invocationCallOrder[0]).toBeLessThan(
    onTakeSelected.mock.invocationCallOrder[0],
  );
});

test("a take the Engine refuses is reported, and nothing is re-read", async () => {
  const refuse = vi.fn<EngineClient["selectTake"]>(async () => {
    throw new Error("That Block is no longer in the Narration.");
  });
  const { onTakeSelected } = mount(reading(REPORT), refuse);
  fireEvent.click(screen.getByRole("button", { name: "Regenerate this Block" }));
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toBe("That Block is no longer in the Narration.");
  expect(onTakeSelected).not.toHaveBeenCalled();
});

test("a Block with no second take offers only the re-roll", () => {
  mount(reading({ ...REPORT, playingBlock: { ...BLOCK, hasComparison: false } }));
  expect(screen.getByRole("button", { name: "Regenerate this Block" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Play A · stored take" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Play B · re-roll" })).toBeNull();
});
