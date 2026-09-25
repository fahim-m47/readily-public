import { act, renderHook } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ControlSettings, Overrides } from "../engine/advanced";
import type { VoiceSelection } from "../engine/client";
import { useControls } from "./useControls";

const named = (voiceId: string): ControlSettings => ({
  overrides: {},
  effectiveValues: { word_timing: voiceId },
});

const pending = () => {
  const waiting = new Map<string, (settings: ControlSettings) => void>();
  const failing = new Map<string, (error: Error) => void>();
  const controls = vi.fn(
    ({ voiceId }: VoiceSelection) =>
      new Promise<ControlSettings>((resolve, reject) => {
        waiting.set(voiceId, resolve);
        failing.set(voiceId, reject);
      }),
  );
  const saving = new Map<string, () => void>();
  const setControls = vi.fn(
    (voice: VoiceSelection, overrides: Overrides) =>
      new Promise<ControlSettings>((resolve) => {
        saving.set(voice.voiceId, () =>
          resolve({
            overrides,
            effectiveValues: { ...named(voice.voiceId).effectiveValues, ...overrides },
          }),
        );
      }),
  );
  return { client: { controls, setControls }, waiting, failing, saving };
};

const mount = (client: ReturnType<typeof pending>["client"], voiceId: string) =>
  renderHook(({ voice }: { voice: VoiceSelection }) => useControls(client, voice), {
    initialProps: { voice: { modelId: "supertonic:66m", voiceId } },
  });

test("one Voice's controls are never shown under another Voice's name", async () => {
  const { client, waiting } = pending();
  const { result, rerender } = mount(client, "M1");

  await act(async () => waiting.get("M1")?.(named("M1")));
  expect(result.current.settings?.effectiveValues.word_timing).toBe("M1");

  rerender({ voice: { modelId: "supertonic:66m", voiceId: "M2" } });
  expect(result.current.settings).toBe(null);

  await act(async () => waiting.get("M2")?.(named("M2")));
  expect(result.current.settings?.effectiveValues.word_timing).toBe("M2");
});

test("a read that arrives after the reader has moved on publishes nothing", async () => {
  const { client, waiting } = pending();
  const { result, rerender } = mount(client, "M1");
  rerender({ voice: { modelId: "supertonic:66m", voiceId: "M2" } });

  await act(async () => waiting.get("M1")?.(named("M1")));
  expect(result.current.settings).toBe(null);
});

test("a read that fails leaves a sentence about that Voice and no settings", async () => {
  const { client, failing } = pending();
  const { result } = mount(client, "M1");

  await act(async () => failing.get("M1")?.(new Error("offline")));
  expect(result.current.settings).toBe(null);
  expect(result.current.failure).toBe(
    "Controls could not be read. Select a different Voice and come back to try again.",
  );
});

test("saving replaces what is shown with the Engine's own answer", async () => {
  const { client, waiting, saving } = pending();
  const { result } = mount(client, "M1");
  await act(async () => waiting.get("M1")?.(named("M1")));

  await act(async () => {
    const settled = result.current.save({ prepare_first: true });
    saving.get("M1")?.();
    await settled;
  });
  expect(client.setControls).toHaveBeenCalledWith(
    { modelId: "supertonic:66m", voiceId: "M1" },
    { prepare_first: true },
  );
  expect(result.current.settings?.overrides).toEqual({ prepare_first: true });
  expect(result.current.settings?.effectiveValues.prepare_first).toBe(true);
});

test("a save that lands after the reader has moved on leaves the new Voice showing", async () => {
  const { client, waiting, saving } = pending();
  const { result, rerender } = mount(client, "M1");
  await act(async () => waiting.get("M1")?.(named("M1")));

  let settled: Promise<void> | null = null;
  act(() => {
    settled = result.current.save({ prepare_first: true });
  });

  rerender({ voice: { modelId: "supertonic:66m", voiceId: "M2" } });
  await act(async () => waiting.get("M2")?.(named("M2")));

  await act(async () => {
    saving.get("M1")?.();
    await settled;
  });
  expect(result.current.settings?.effectiveValues.word_timing).toBe("M2");
  expect(result.current.failure).toBe(null);
});
