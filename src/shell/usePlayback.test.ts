import { act, renderHook } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { Diagnostics } from "../engine/advanced";
import type { EngineClient, HistoryNarration, HistorySegment, NarrationState } from "../engine/client";
import type { EngineBinding } from "../engine/useEngine";
import { usePlayback } from "./usePlayback";

const narration = (id: string) =>
  ({ id, sourcePreview: id, source: id, segments: [], gaps: [] }) as unknown as HistoryNarration;

const B = narration("B");

const playing = (narrationId: string, over: Partial<NarrationState> = {}) =>
  ({ narration: { narrationId, totalSec: 0, ...over } as NarrationState }) as EngineBinding;

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

const onBlock = (ordinal: number): Diagnostics => ({
  ...QUIET,
  playingBlock: {
    ordinal,
    recordHash: String(ordinal).repeat(64),
    seed: ordinal,
    cacheHit: false,
    wordTiming: "estimated",
    supportModel: null,
    take: "A",
    hasComparison: false,
  },
});

const client = {
  openNarration: vi.fn(async (id: string) => narration(id)),
} as unknown as EngineClient;

test("closing a second Narration does not bring back the first one closed", async () => {
  const { result, rerender } = renderHook(
    ({ opened }: { opened: HistoryNarration | null }) => usePlayback(client, playing("A"), opened),
    { initialProps: { opened: null as HistoryNarration | null } },
  );
  await act(async () => {});
  expect(result.current.narration?.id).toBe("A");

  act(() => result.current.dismiss());
  expect(result.current.narration).toBe(null);

  act(() => result.current.reveal("B"));
  rerender({ opened: B });
  await act(async () => {});
  expect(result.current.narration?.id).toBe("B");

  act(() => result.current.dismiss());
  rerender({ opened: null });
  await act(async () => {});
  expect(result.current.narration).toBe(null);
  expect(result.current.playerNarration?.id).toBe("A");
});

const segment = (ordinal: number, settled: boolean): HistorySegment => ({
  ordinal,
  sourceStart: ordinal * 4,
  sourceEnd: ordinal * 4 + 3,
  boundary: "sentence",
  durationSec: settled ? 1 : null,
  audioPresent: settled,
  timelineStartSec: settled ? ordinal : null,
  timings: [],
});

test("a Narration walking its Blocks is read once; assembling and a take swap read again", async () => {
  const openNarration = vi.fn(async (id: string, options?: { afterOrdinal: number }) =>
    options
      ? { ...narration(id), source: undefined, segments: [segment(1, true), segment(2, false)] }
      : { ...narration(id), segments: [segment(0, true), segment(1, false)] },
  );
  const reading = { openNarration } as unknown as EngineClient;
  const { result, rerender } = renderHook(
    ({ state }: { state: Partial<NarrationState> }) =>
      usePlayback(reading, playing("A", state), null),
    { initialProps: { state: { totalSec: 0, diagnostics: onBlock(0) } } },
  );
  await act(async () => {});
  expect(openNarration).toHaveBeenCalledTimes(1);

  rerender({ state: { totalSec: 0, diagnostics: onBlock(1) } });
  await act(async () => {});
  rerender({ state: { totalSec: 0, diagnostics: onBlock(2) } });
  await act(async () => {});
  expect(openNarration).toHaveBeenCalledTimes(1);

  rerender({ state: { totalSec: 12, diagnostics: onBlock(2) } });
  await act(async () => {});
  expect(openNarration).toHaveBeenCalledTimes(2);
  expect(openNarration).toHaveBeenLastCalledWith("A", { afterOrdinal: 0 });
  expect(result.current.narration?.source).toBe("A");
  expect(result.current.narration?.segments.map((s) => [s.ordinal, s.durationSec])).toEqual([
    [0, 1],
    [1, 1],
    [2, null],
  ]);

  act(() => result.current.reread());
  await act(async () => {});
  expect(openNarration).toHaveBeenCalledTimes(3);
  expect(openNarration.mock.calls[2]).toEqual(["A"]);
});

test("a gap is not settled: the cursor stops before it, and a Resume that fills it is read", async () => {
  const gap = { ordinal: 1, sourceStart: 4, sourceEnd: 7, errorCode: "synthesis_failed", createdAt: "" };
  const replies = [
    { ...narration("A"), segments: [segment(0, true), segment(1, false), segment(2, true)], gaps: [gap] },
    { ...narration("A"), source: undefined, segments: [segment(1, true), segment(2, true), segment(3, false)], gaps: [] },
  ];
  const openNarration = vi.fn(async () => replies.shift());
  const reading = { openNarration } as unknown as EngineClient;
  const { result, rerender } = renderHook(
    ({ totalSec }: { totalSec: number }) => usePlayback(reading, playing("A", { totalSec }), null),
    { initialProps: { totalSec: 0 } },
  );
  await act(async () => {});

  rerender({ totalSec: 2 });
  await act(async () => {});
  expect(openNarration).toHaveBeenLastCalledWith("A", { afterOrdinal: 0 });
  expect(result.current.narration?.segments.map((s) => [s.ordinal, s.durationSec])).toEqual([
    [0, 1],
    [1, 1],
    [2, 1],
    [3, null],
  ]);
});

test("an Engine that sends the prefix back replaces what the shell held", async () => {
  const replies = [
    { ...narration("A"), segments: [segment(0, true), segment(1, true), segment(2, false)], gaps: [] },
    { ...narration("A"), source: undefined, segments: [segment(0, false), segment(1, true), segment(2, true)], gaps: [] },
  ];
  const openNarration = vi.fn(async () => replies.shift());
  const reading = { openNarration } as unknown as EngineClient;
  const { result, rerender } = renderHook(
    ({ totalSec }: { totalSec: number }) => usePlayback(reading, playing("A", { totalSec }), null),
    { initialProps: { totalSec: 0 } },
  );
  await act(async () => {});

  rerender({ totalSec: 2 });
  await act(async () => {});
  expect(openNarration).toHaveBeenLastCalledWith("A", { afterOrdinal: 1 });
  expect(result.current.narration?.source).toBe("A");
  expect(result.current.narration?.segments.map((s) => [s.ordinal, s.durationSec])).toEqual([
    [0, null],
    [1, 1],
    [2, 1],
  ]);
});
