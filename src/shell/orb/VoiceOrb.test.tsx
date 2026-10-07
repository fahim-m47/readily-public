import { render } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { VoiceOrb } from "./VoiceOrb";

const loseContext = vi.fn();

// jsdom has no WebGL. The fake answers every call, reports every shader as
// compiled, and records when the orb gives its context back.
const gl = new Proxy({}, {
  get: (_, name) => {
    if (name === "getExtension") return () => ({ loseContext });
    if (name === "isContextLost") return () => false;
    if (name === "getShaderParameter" || name === "getProgramParameter") return () => true;
    return () => ({});
  },
});

class Observer {
  observe() {}
  disconnect() {}
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(gl as WebGLRenderingContext);
  vi.stubGlobal("ResizeObserver", Observer);
  vi.stubGlobal("IntersectionObserver", Observer);
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  loseContext.mockClear();
});

const colors = ["#287cf4", "#aa9aee", "#ffd1bc", "#85d7ef"] as const;

test("a StrictMode remount keeps the canvas's context, and a real unmount frees it", () => {
  const { container, unmount } = render(
    <StrictMode><VoiceOrb colors={[...colors]} animated={false} /></StrictMode>,
  );
  vi.runAllTimers();
  expect(loseContext).not.toHaveBeenCalled();
  expect(container.querySelector("canvas")?.style.background).toBe("none");

  unmount();
  vi.runAllTimers();
  expect(loseContext).toHaveBeenCalledOnce();
});
