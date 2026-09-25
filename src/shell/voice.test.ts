import { expect, test } from "vitest";
import type { CatalogEntry, ModelStatus } from "../engine/client";
import {
  describeSelection,
  installedEntries,
  isSelected,
} from "./voice";

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
  voices: [
    { simple: true, id: "af_heart", name: "Heart", language: "en-US", preview: null },
    { simple: true, id: "af_bella", name: "Bella", language: "en-US", preview: null },
  ],
  defaultVoiceId: "af_heart",
  downloadBytes: 1024 * 1024 * 337,
};

const QWEN: CatalogEntry = {
  id: "qwen3-tts:0.6b",
  name: "Qwen3 TTS",
  tier: "expressive",
  supportModels: [],
  licenseTerms: {
      id: "Apache-2.0",
      name: "Apache 2.0",
      bindsReader: false,
      credit: null,
      attribution: null,
      text: "Apache License\nVersion 2.0, January 2004",
  },
  ramClassGb: 3,
  voices: [{ simple: true, id: "Chelsie", name: "Chelsie", language: "en-US", preview: null }],
  defaultVoiceId: "Chelsie",
  downloadBytes: 1024 * 1024 * 1700,
};

const store = (statuses: Partial<Record<string, boolean>>) => (entryId: string) =>
  statuses[entryId] === undefined
    ? undefined
    : ({
        id: entryId,
        installed: statuses[entryId],
        diskBytes: 0,
        downloadBytes: 0,
      } as ModelStatus);

test("the popover lists what is on disk, in the Catalog's order", () => {
  const listed = installedEntries(
    [KOKORO, QWEN],
    store({ "kokoro:82m": true, "qwen3-tts:0.6b": true }),
  );

  expect(listed.map((item) => item.entry.name)).toEqual(["Kokoro", "Qwen3 TTS"]);
  expect(listed[0].voices.map((voice) => voice.name)).toEqual(["Heart", "Bella"]);
});

test("a Voice Model the store has not answered for yet is not offered", () => {
  expect(installedEntries([KOKORO, QWEN], store({ "kokoro:82m": false }))).toEqual([]);
  expect(installedEntries(null, store({}))).toEqual([]);
});

test("the pill names the Voice Model and the Voice, as the Catalog names them", () => {
  expect(
    describeSelection([KOKORO, QWEN], {
      modelId: "kokoro:82m",
      voiceId: "af_bella",
    }),
  ).toBe("Kokoro · Bella");
});

test("the pill says nothing until it can name something", () => {
  const chosen = { modelId: "kokoro:82m", voiceId: "af_heart" };

  expect(describeSelection(null, chosen)).toBe(null);
  expect(describeSelection([KOKORO], null)).toBe(null);
  expect(describeSelection([QWEN], chosen)).toBe(null);
  expect(
    describeSelection([KOKORO], { modelId: "kokoro:82m", voiceId: "af_gone" }),
  ).toBe(null);
});

test("the chosen Voice is a pair, not a Voice Model", () => {
  const chosen = { modelId: "kokoro:82m", voiceId: "af_heart" };

  expect(isSelected(chosen, "kokoro:82m", "af_heart")).toBe(true);
  expect(isSelected(chosen, "kokoro:82m", "af_bella")).toBe(false);
  expect(isSelected(null, "kokoro:82m", "af_heart")).toBe(false);
});
