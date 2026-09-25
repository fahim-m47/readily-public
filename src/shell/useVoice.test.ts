import { act, renderHook } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { EngineClient, VoiceSelection } from "../engine/client";
import type { EngineBinding } from "../engine/useEngine";
import { useVoice } from "./useVoice";

const CHELSIE: VoiceSelection = { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" };
const ETHAN: VoiceSelection = { modelId: "qwen3-tts:0.6b", voiceId: "Ethan" };

const ready = { connection: { state: "ready" } } as EngineBinding;

test("a read that loses to a switch already stored does not undo it", async () => {
  let refuse = () => {};
  const client = {
    voiceSelection: vi.fn(
      () =>
        new Promise<VoiceSelection>((_resolve, reject) => {
          refuse = () => reject(new Error("The voice settings table is missing."));
        }),
    ),
    selectVoice: vi.fn(async (chosen: VoiceSelection) => chosen),
  } as unknown as EngineClient;

  const { result } = renderHook(() => useVoice(client, ready));
  await act(async () => result.current.select(CHELSIE));
  expect(result.current.selection).toEqual(CHELSIE);

  await act(async () => refuse());

  expect(result.current.selection).toEqual(CHELSIE);
  expect(result.current.readFailed).toBe(false);
  expect(result.current.notice).toBe(null);
});

test("a read the Engine refuses after a restart is not covered by the one before it", async () => {
  let reads = 0;
  const client = {
    voiceSelection: vi.fn(async () => {
      reads += 1;
      if (reads === 1) return CHELSIE;
      throw new Error("The voice settings table is missing.");
    }),
    selectVoice: vi.fn(async (chosen: VoiceSelection) => chosen),
  } as unknown as EngineClient;

  const { result, rerender } = renderHook(
    ({ engine }: { engine: EngineBinding }) => useVoice(client, engine),
    { initialProps: { engine: ready } },
  );
  await act(async () => {});
  expect(result.current.selection).toEqual(CHELSIE);

  rerender({
    engine: { connection: { state: "starting", detail: "restarting" } } as EngineBinding,
  });
  rerender({ engine: ready });
  await act(async () => {});

  expect(result.current.readFailed).toBe(true);
  expect(result.current.selection).toBe(null);
  expect(result.current.notice).toBe("The voice settings table is missing.");
});

test("a switch that lands after a restart read began cannot cover for that read", async () => {
  let store = () => {};
  let refuse = () => {};
  let reads = 0;
  const client = {
    voiceSelection: vi.fn(() => {
      reads += 1;
      if (reads === 1) return Promise.resolve(CHELSIE);
      return new Promise<VoiceSelection>((_resolve, reject) => {
        refuse = () => reject(new Error("The voice settings table is missing."));
      });
    }),
    selectVoice: vi.fn(
      (chosen: VoiceSelection) =>
        new Promise<VoiceSelection>((resolve) => {
          store = () => resolve(chosen);
        }),
    ),
  } as unknown as EngineClient;

  const { result, rerender } = renderHook(
    ({ engine }: { engine: EngineBinding }) => useVoice(client, engine),
    { initialProps: { engine: ready } },
  );
  await act(async () => {});
  act(() => result.current.select(ETHAN));

  rerender({
    engine: { connection: { state: "starting", detail: "restarting" } } as EngineBinding,
  });
  rerender({ engine: ready });
  await act(async () => store());
  expect(result.current.selection).toEqual(ETHAN);

  await act(async () => refuse());

  expect(result.current.readFailed).toBe(true);
  expect(result.current.selection).toBe(null);
});

test("a switch stored after the restart read answered is the Engine's latest word", async () => {
  let store = () => {};
  const client = {
    voiceSelection: vi.fn(async () => CHELSIE),
    selectVoice: vi.fn(
      (chosen: VoiceSelection) =>
        new Promise<VoiceSelection>((resolve) => {
          store = () => resolve(chosen);
        }),
    ),
  } as unknown as EngineClient;

  const { result, rerender } = renderHook(
    ({ engine }: { engine: EngineBinding }) => useVoice(client, engine),
    { initialProps: { engine: ready } },
  );
  await act(async () => {});
  act(() => result.current.select(ETHAN));

  rerender({
    engine: { connection: { state: "starting", detail: "restarting" } } as EngineBinding,
  });
  rerender({ engine: ready });
  await act(async () => {});
  expect(result.current.selection).toEqual(CHELSIE);

  await act(async () => store());

  expect(result.current.selection).toEqual(ETHAN);
  expect(result.current.readFailed).toBe(false);
});

test("a switch stored after a refused restart read also clears its notice", async () => {
  let store = () => {};
  let reads = 0;
  const client = {
    voiceSelection: vi.fn(async () => {
      reads += 1;
      if (reads === 1) return CHELSIE;
      throw new Error("The voice settings table is missing.");
    }),
    selectVoice: vi.fn(
      (chosen: VoiceSelection) =>
        new Promise<VoiceSelection>((resolve) => {
          store = () => resolve(chosen);
        }),
    ),
  } as unknown as EngineClient;

  const { result, rerender } = renderHook(
    ({ engine }: { engine: EngineBinding }) => useVoice(client, engine),
    { initialProps: { engine: ready } },
  );
  await act(async () => {});
  act(() => result.current.select(ETHAN));

  rerender({
    engine: { connection: { state: "starting", detail: "restarting" } } as EngineBinding,
  });
  rerender({ engine: ready });
  await act(async () => {});
  expect(result.current.readFailed).toBe(true);

  await act(async () => store());

  expect(result.current.selection).toEqual(ETHAN);
  expect(result.current.notice).toBe(null);
});
