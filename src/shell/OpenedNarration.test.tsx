import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { HistoryNarration } from "../engine/client";
import OpenedNarration from "./OpenedNarration";
import { readAlongBlocks } from "./readalong";

// Every render of the memoized Block, so a test can see which Blocks a
// playhead move touched.
const blockRenders = vi.hoisted(() => [] as { current: boolean }[]);
vi.mock("react", async (importOriginal) => {
  const react = await importOriginal<typeof import("react")>();
  const memo = (component: (props: { current: boolean }) => React.ReactNode) =>
    react.memo((props: { current: boolean }) => {
      blockRenders.push(props);
      return component(props);
    });
  return { ...react, memo };
});

afterEach(cleanup);

const narration: HistoryNarration = {
  id: "words", source: "🌊 Hello world.", sourcePreview: "🌊 Hello world.",
  modelId: "kokoro:82m", voiceId: "af_heart", speed: 1, status: "playing",
  createdAt: "2026-09-06", updatedAt: "2026-09-06", lastPlayedAt: "2026-09-06",
  playheadSec: 1, totalDurationSec: 2, audioPresent: true, hasGaps: false, gaps: [],
  segments: [{
    ordinal: 0, sourceStart: 0, sourceEnd: 14, boundary: "paragraph",
    durationSec: 2, audioPresent: true, timelineStartSec: 0,
    timings: [{ sourceStart: 8, sourceEnd: 13, startSec: 1, endSec: 1.5, provenance: "estimated" }],
  }],
};

test("timed words highlight only during their interval and click in Source coordinates", () => {
  const onSeek = vi.fn();
  const props = { blocks: readAlongBlocks(narration), onSeek };
  const { container, rerender } = render(<OpenedNarration {...props} positionSec={1.2} />);
  const word = screen.getByRole("button", { name: "world" });
  expect(word.getAttribute("aria-current")).toBe("true");
  expect(word.title).toBe("Play from near this word");
  fireEvent.click(word);
  expect(onSeek).toHaveBeenCalledWith(8);
  expect(container.querySelector(".opened__source")?.textContent).toBe(narration.source);
  rerender(<OpenedNarration {...props} positionSec={1.5} />);
  expect(word.hasAttribute("aria-current")).toBe(false);
});

test("words remain individually seekable before timings are available", () => {
  const untimed = { ...narration, segments: narration.segments.map(segment => ({ ...segment, timings: [] })) };
  const onSeek = vi.fn();
  render(<OpenedNarration blocks={readAlongBlocks(untimed)}
    positionSec={1.2} onSeek={onSeek} />);
  const word = screen.getByRole("button", { name: "world" });
  fireEvent.click(word);
  expect(onSeek).toHaveBeenCalledWith(8);
});

test.each([1, null])("an unmeasured target is seekable only after a measured prefix (%s)", durationSec => {
  const pending: HistoryNarration = {
    ...narration, source: "First. Hello world.",
    segments: [
      { ...narration.segments[0], sourceEnd: 6, durationSec, timings: [] },
      { ...narration.segments[0], ordinal: 1, sourceStart: 7, sourceEnd: 19,
        durationSec: null, timelineStartSec: null, audioPresent: false, timings: [] },
    ],
  };
  const onSeek = vi.fn();
  render(<OpenedNarration blocks={readAlongBlocks(pending)}
    positionSec={0} onSeek={onSeek} />);
  if (durationSec === null) {
    expect(screen.queryByRole("button", { name: "world" })).toBeNull();
  } else {
    fireEvent.click(screen.getByRole("button", { name: "world" }));
    expect(onSeek).toHaveBeenCalledWith(13);
  }
});

test("a Block behind the playhead keeps its played tone whether or not its words are timed", () => {
  const two: HistoryNarration = {
    ...narration, source: "First. Hello world.",
    segments: [
      { ...narration.segments[0], sourceEnd: 6, timings: [] },
      { ...narration.segments[0], ordinal: 1, sourceStart: 7, sourceEnd: 19, timelineStartSec: 2,
        timings: [{ sourceStart: 13, sourceEnd: 18, startSec: 3, endSec: 3.5, provenance: "spoken" }] },
    ],
  };
  const { container } = render(<OpenedNarration blocks={readAlongBlocks(two)} positionSec={3.2} onSeek={vi.fn()} />);
  const [first, second] = container.querySelectorAll(".opened__source > .opened__block");
  expect(first.classList.contains("opened__block--played")).toBe(true);
  expect(second.classList.contains("opened__block--played")).toBe(false);
  expect(second.hasAttribute("aria-current")).toBe(false);
  expect(screen.getByRole("button", { name: "world" }).getAttribute("aria-current")).toBe("true");
});

test("a Block with a gap marks the skipped text and keeps its other words seekable", () => {
  const gapped: HistoryNarration = {
    ...narration, hasGaps: true,
    gaps: [{ ordinal: 0, sourceStart: 2, sourceEnd: 7, errorCode: "generation_failed", createdAt: "2026-09-06" }],
  };
  const onSeek = vi.fn();
  const { container } = render(<OpenedNarration blocks={readAlongBlocks(gapped)} positionSec={null} onSeek={onSeek} />);
  expect(container.querySelector("mark.opened__skipped")?.textContent).toContain("Hello");
  expect(screen.queryByRole("button", { name: "Hello" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "world" }));
  expect(onSeek).toHaveBeenCalledWith(8);
  expect(container.querySelector(".opened__source")?.textContent).toContain("🌊 ");
});

test("moving the playhead re-renders only the Blocks it left and entered", () => {
  const two: HistoryNarration = {
    ...narration, source: "First. Hello world.",
    segments: [
      { ...narration.segments[0], sourceEnd: 6, timings: [] },
      { ...narration.segments[0], ordinal: 1, sourceStart: 7, sourceEnd: 19, timelineStartSec: 2,
        timings: [{ sourceStart: 13, sourceEnd: 18, startSec: 3, endSec: 3.5, provenance: "spoken" }] },
    ],
  };
  const props = { blocks: readAlongBlocks(two), onSeek: vi.fn() };
  const { container, rerender } = render(<OpenedNarration {...props} positionSec={0.5} />);
  const [first] = container.querySelectorAll(".opened__source > .opened__block");
  expect(first.getAttribute("aria-current")).toBe("true");

  blockRenders.length = 0;
  rerender(<OpenedNarration {...props} positionSec={0.7} />);
  expect(blockRenders).toEqual([]);
  rerender(<OpenedNarration {...props} positionSec={3.2} />);
  expect(blockRenders.map((block) => block.current)).toEqual([false, true]);
  expect(first.hasAttribute("aria-current")).toBe(false);
  expect(first.classList.contains("opened__block--played")).toBe(true);
  expect(screen.getByRole("button", { name: "world" }).getAttribute("aria-current")).toBe("true");
});

test("a refetch with one more settled Segment re-renders only the Blocks at the tail", () => {
  const segment = (ordinal: number, sourceStart: number, sourceEnd: number, settled: boolean) => ({
    ...narration.segments[0], ordinal, sourceStart, sourceEnd, timings: [],
    durationSec: settled ? 1 : null, timelineStartSec: settled ? ordinal : null, audioPresent: settled,
  });
  const source = "First. Hello world. Then more.";
  const first = segment(0, 0, 6, true);
  const before: HistoryNarration = {
    ...narration, source, segments: [first, segment(1, 7, 19, false), segment(2, 20, 30, false)],
  };
  const after: HistoryNarration = {
    ...before, gaps: [], segments: [first, segment(1, 7, 19, true), segment(2, 20, 30, false)],
  };
  const onSeek = vi.fn();
  const blocks = readAlongBlocks(before);
  const { container, rerender } = render(<OpenedNarration blocks={blocks} positionSec={0} onSeek={onSeek} />);

  blockRenders.length = 0;
  const next = readAlongBlocks(after, blocks);
  expect(next[0]).toBe(blocks[0]);
  rerender(<OpenedNarration blocks={next} positionSec={0} onSeek={onSeek} />);
  expect(blockRenders).toHaveLength(2);
  expect(container.querySelector(".opened__source")?.textContent).toBe(source);
  fireEvent.click(screen.getByRole("button", { name: "more" }));
  expect(onSeek).toHaveBeenCalledWith(25);
});
