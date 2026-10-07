import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { EngineClient, UpdateStatus } from "../engine/client";
import { useUpdate } from "./useUpdate";

afterEach(() => vi.useRealTimers());

// Only the commands the hook reads; the rest of the client is nothing to
// do with updates, so the cast keeps the fixture the size of the question.
const supervisor = (first: UpdateStatus) => {
  let answering = first;
  const updateStatus = vi.fn(async () => answering);
  const installUpdate = vi.fn(async () => {
    answering = { state: "installing" };
  });
  const openDownloadPage = vi.fn(async () => {});
  const client = { updateStatus, installUpdate, openDownloadPage } as unknown as EngineClient;
  return {
    client,
    updateStatus,
    installUpdate,
    openDownloadPage,
    // What the next poll finds.
    serve: (next: UpdateStatus) => {
      answering = next;
    },
  };
};

const OFFER: UpdateStatus = {
  state: "available",
  version: "0.2.0",
  notes: "Faster.",
};

test("nothing published is nothing to ask about", async () => {
  const { client, updateStatus } = supervisor({ state: "idle" });
  const { result } = renderHook(() => useUpdate(client));

  await waitFor(() => expect(updateStatus).toHaveBeenCalled());
  expect(result.current.offer).toBe(null);
});

test("an offer that lands after the screen is up still reaches the reader", async () => {
  vi.useFakeTimers();
  const { client, serve } = supervisor({ state: "idle" });
  const { result } = renderHook(() => useUpdate(client));

  await act(async () => {});
  expect(result.current.offer).toBe(null);

  serve(OFFER);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(result.current.offer).toEqual({
    version: "0.2.0",
    notes: "Faster.",
    installable: true,
    installing: false,
    failure: null,
  });
});

test("the reader's yes is the only thing that installs anything", async () => {
  const { client, installUpdate } = supervisor(OFFER);
  const { result } = renderHook(() => useUpdate(client));

  await waitFor(() => expect(result.current.offer).not.toBe(null));
  expect(installUpdate).not.toHaveBeenCalled();

  await act(async () => result.current.install());
  expect(installUpdate).toHaveBeenCalledWith();
  expect(result.current.offer?.installing).toBe(true);
});

test("a status read already in flight cannot undo the reader's yes", async () => {
  vi.useFakeTimers();
  const { client } = supervisor(OFFER);
  const { result } = renderHook(() => useUpdate(client));

  await act(async () => {});
  await act(async () => result.current.install());
  expect(result.current.offer?.installing).toBe(true);

  // The supervisor is mid-install, but a poll asked before the reader
  // pressed anything still answers with the offer it knew about.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(result.current.offer?.installing).toBe(true);
});

test("a failed install leaves the offer on screen to try again", async () => {
  vi.useFakeTimers();
  const { client, serve } = supervisor(OFFER);
  const { result } = renderHook(() => useUpdate(client));

  await act(async () => {});
  serve({ state: "failed", reason: "The download did not finish.", untouched: true });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });

  expect(result.current.offer).toEqual({
    version: "0.2.0",
    notes: "Faster.",
    installable: true,
    installing: false,
    failure: { reason: "The download did not finish.", untouched: true },
  });
});

test("a release this copy cannot install is announced, with no install to take", async () => {
  const { client, installUpdate, openDownloadPage } = supervisor({
    state: "announced",
    version: "0.2.0",
    notes: "Faster.",
  });
  const { result } = renderHook(() => useUpdate(client));

  await waitFor(() =>
    expect(result.current.offer).toEqual({
      version: "0.2.0",
      notes: "Faster.",
      installable: false,
      installing: false,
      failure: null,
    }),
  );

  await act(async () => result.current.openDownloadPage());
  expect(openDownloadPage).toHaveBeenCalledWith();
  expect(installUpdate).not.toHaveBeenCalled();
});

test("later means gone for this run, whatever the supervisor keeps answering", async () => {
  vi.useFakeTimers();
  const { client } = supervisor(OFFER);
  const { result } = renderHook(() => useUpdate(client));

  await act(async () => {});
  expect(result.current.offer).not.toBe(null);

  act(() => result.current.dismiss());
  expect(result.current.offer).toBe(null);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(result.current.offer).toBe(null);
});

test("a bridge that cannot answer says nothing to the reader", async () => {
  const updateStatus = vi.fn(async () => {
    throw new Error("no bridge");
  });
  const client = { updateStatus, installUpdate: vi.fn() } as unknown as EngineClient;
  const { result } = renderHook(() => useUpdate(client));

  await waitFor(() => expect(updateStatus).toHaveBeenCalled());
  expect(result.current.offer).toBe(null);
});

test("the poll stops when the screen goes away", async () => {
  vi.useFakeTimers();
  const { client, updateStatus } = supervisor({ state: "idle" });
  const { unmount } = renderHook(() => useUpdate(client));

  await act(async () => {});
  const asked = updateStatus.mock.calls.length;
  unmount();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  expect(updateStatus).toHaveBeenCalledTimes(asked);
});
