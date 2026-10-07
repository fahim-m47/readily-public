import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { CatalogEntry, ModelStatus } from "../engine/client";
import ModelMenu from "./ModelMenu";

afterEach(cleanup);

const KOKORO: CatalogEntry = {
  id: "kokoro:82m",
  name: "Kokoro",
  tier: "instant",
  supportModels: [],
  licenseTerms: {
    id: "Apache-2.0",
    name: "Apache 2.0",
    bindsReader: false,
    credit: null,
    attribution: null,
    text: "Apache License\nVersion 2.0, January 2004",
  },
  ramClassGb: 0.5,
  voices: [{ simple: true, id: "af_heart", name: "Heart", language: "en-US", preview: null }],
  defaultVoiceId: "af_heart",
  downloadBytes: 1024 * 1024 * 337,
  runsHere: true,
};

const QWEN: CatalogEntry = {
  ...KOKORO,
  id: "qwen3-tts:0.6b",
  name: "Qwen3 TTS",
  tier: "expressive",
  ramClassGb: 3,
  voices: [{ simple: true, id: "Chelsie", name: "Chelsie", language: "en-US", preview: null }],
  defaultVoiceId: "Chelsie",
  downloadBytes: 1024 * 1024 * 1700,
  runsHere: false,
};

const installed = (entry: CatalogEntry): ModelStatus => ({
  id: entry.id,
  installed: true,
  diskBytes: entry.downloadBytes,
  downloadBytes: entry.downloadBytes,
});

test("a model on disk that this machine cannot run is not pickable, only deletable", () => {
  // The Apple-silicon build can fill a data directory the Intel build then
  // opens. Picking what it left there would store a Voice the Engine then
  // refuses to narrate with.
  const onSelect = vi.fn();
  const onBrowse = vi.fn();
  const statuses = { [KOKORO.id]: installed(KOKORO), [QWEN.id]: installed(QWEN) };
  render(
    <ModelMenu
      entries={[KOKORO, QWEN]}
      statusOf={(entryId) => statuses[entryId]}
      downloads={{ download: null, notice: null, start: vi.fn(), remove: vi.fn(), withdraw: vi.fn() }}
      selection={{ modelId: KOKORO.id, voiceId: "af_heart" }}
      onSelect={onSelect}
      onBrowse={onBrowse}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "Voice Model: Kokoro" }));
  const row = screen.getByText("Qwen3 TTS").closest<HTMLElement>(".model-menu__row");
  if (row === null) throw new Error("the Qwen row did not render");
  expect(row.classList).toContain("model-menu__row--unrunnable");
  expect(within(row).getByRole("button", { name: "Delete Qwen3 TTS" })).toBeTruthy();

  fireEvent.click(within(row).getByText("Qwen3 TTS"));

  expect(onSelect).not.toHaveBeenCalled();
  expect(onBrowse).toHaveBeenCalledTimes(1);
});
