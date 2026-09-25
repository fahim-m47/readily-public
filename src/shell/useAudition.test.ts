import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useAudition } from "./useAudition";

// jsdom has no Web Audio and no media stack, so the tap is played by a fake
// graph that records what the hook did to it. Everything lands in one
// ordered log, because most of what can go wrong here is an ordering
// mistake: a fade from the last Voice suspending the context under the next
// one, or a clip paused before its ramp has run.
const log: string[] = [];

class FakeParam {
  value = 1;
  // Each entry is one scheduled event, so a test can say a clip's gain was
  // held where it was and then ramped to silence — and that another clip's
  // gain was never touched at all.
  readonly events: string[] = [];

  setValueAtTime(value: number, time: number) {
    this.value = value;
    this.events.push(`hold ${value}@${time}`);
  }

  linearRampToValueAtTime(value: number, time: number) {
    this.value = value;
    this.events.push(`ramp ${value}@${time}`);
  }
}

class FakeGain {
  readonly gain = new FakeParam();
  connected = false;
  connect() {
    this.connected = true;
  }
  disconnect() {
    this.connected = false;
  }
}

class FakeSource {
  connected = false;
  constructor(readonly element: HTMLAudioElement) {}
  connect() {
    this.connected = true;
  }
  disconnect() {
    this.connected = false;
  }
}

class FakeContext {
  state = "running";
  currentTime = 0;
  readonly destination = {};
  readonly gains: FakeGain[] = [];
  readonly sources: FakeSource[] = [];

  constructor() {
    made.push(this);
  }

  createAnalyser() {
    return {
      fftSize: 1024,
      connect: () => undefined,
      getFloatTimeDomainData: (samples: Float32Array) => samples.fill(0.5),
    };
  }

  createGain() {
    const gain = new FakeGain();
    this.gains.push(gain);
    return gain;
  }

  createMediaElementSource(element: HTMLAudioElement) {
    const source = new FakeSource(element);
    this.sources.push(source);
    return source;
  }

  resume() {
    this.state = "running";
    log.push("resume");
    return Promise.resolve();
  }

  suspend() {
    this.state = "suspended";
    log.push("suspend");
    return Promise.resolve();
  }

  close() {
    this.state = "closed";
    log.push("close");
    return Promise.resolve();
  }
}

// Every context the hook has made. It only ever makes one, and this is how
// the test reaches it without the class handing `this` out.
const made: FakeContext[] = [];
const context = () => made[0];

// The clip an element is playing, by the name the test gave it.
const clip = (element: HTMLAudioElement) => element.src.split("/").pop();

beforeEach(() => {
  log.length = 0;
  made.length = 0;
  vi.useFakeTimers();
  vi.stubGlobal("AudioContext", FakeContext);
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(function pause(
    this: HTMLAudioElement,
  ) {
    log.push(`pause ${clip(this)}`);
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const hear = async (audition: ReturnType<typeof useAudition>, key: string) => {
  await act(async () => audition.toggle(key, `${key}.m4a`));
};

test("Stop ramps the clip down before pausing it, then lets the device go", async () => {
  const { result } = renderHook(() => useAudition());
  await hear(result.current, "a");

  await act(async () => result.current.stop());

  // The ramp is scheduled at the press; the pause waits for it to run.
  expect(context().gains[0].gain.events).toEqual(["hold 1@0", "ramp 0@0.04"]);
  expect(log).not.toContain("pause a.m4a");
  expect(context().state).toBe("running");

  await act(async () => vi.advanceTimersByTime(40));

  expect(log).toContain("pause a.m4a");
  expect(context().state).toBe("suspended");
  expect(context().sources[0].connected).toBe(false);
  expect(context().gains[0].connected).toBe(false);
});

test("hearing a second Voice fades the first without touching the second", async () => {
  const { result } = renderHook(() => useAudition());
  await hear(result.current, "a");
  await hear(result.current, "b");

  // A gain each: the first is ramping down while the second plays at full.
  expect(context().gains).toHaveLength(2);
  expect(context().gains[0].gain.events).toEqual(["hold 1@0", "ramp 0@0.04"]);
  expect(context().gains[1].gain.events).toEqual([]);
  // Both are still in the graph: the first is faded out, never cut out.
  expect(context().sources[0].connected).toBe(true);
  expect(context().sources[1].connected).toBe(true);

  // The first clip's fade lands 40ms into the second one.
  await act(async () => vi.advanceTimersByTime(40));

  expect(result.current.playing).toBe("b");
  expect(log).toContain("pause a.m4a");
  expect(log).not.toContain("pause b.m4a");
  expect(context().sources[0].connected).toBe(false);
  expect(context().sources[1].connected).toBe(true);
  // The second clip is still audible: nothing suspended the context after
  // the resume that started it.
  expect(context().state).toBe("running");
  expect(log.slice(log.lastIndexOf("resume"))).not.toContain("suspend");
});

test("a sheet closing mid-clip is not cut off, and the context closes behind it", async () => {
  const { result, unmount } = renderHook(() => useAudition());
  await hear(result.current, "a");

  await act(async () => unmount());

  expect(context().gains[0].gain.events).toEqual(["hold 1@0", "ramp 0@0.04"]);
  expect(log).not.toContain("pause a.m4a");
  expect(log).not.toContain("close");

  await act(async () => vi.advanceTimersByTime(40));

  expect(log.indexOf("pause a.m4a")).toBeGreaterThan(-1);
  expect(log.indexOf("pause a.m4a")).toBeLessThan(log.indexOf("close"));
  expect(context().state).toBe("closed");
});

test("the device is let go by the last fade to finish, not the first", async () => {
  const { result } = renderHook(() => useAudition());
  await hear(result.current, "a");
  await hear(result.current, "b");

  // Stop the second clip while the first is still fading, so two fades are
  // in flight at once and they end 20ms apart.
  await act(async () => vi.advanceTimersByTime(20));
  await act(async () => result.current.stop());

  await act(async () => vi.advanceTimersByTime(20));

  // The first clip has gone quiet, but the second is still fading and the
  // device is still needed.
  expect(log).toContain("pause a.m4a");
  expect(log).not.toContain("pause b.m4a");
  expect(log).not.toContain("suspend");

  await act(async () => vi.advanceTimersByTime(20));

  expect(log.filter((entry) => entry === "suspend")).toHaveLength(1);
  expect(log.indexOf("suspend")).toBeGreaterThan(log.indexOf("pause b.m4a"));
});
