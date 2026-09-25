import { afterEach, expect, test } from "vitest";
import type { CatalogEntry } from "../engine/client";
import { lastVoiceOf, rememberVoice, voiceToOffer } from "./lastVoice";

const KEY = "readily.lastVoice";

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
    text: "Apache License",
  },
  ramClassGb: 0.5,
  voices: [
    { simple: true, id: "af_heart", name: "Heart", language: "en-US", preview: null },
    { simple: true, id: "af_bella", name: "Bella", language: "en-US", preview: null },
  ],
  defaultVoiceId: "af_heart",
  downloadBytes: 1,
};

afterEach(() => localStorage.clear());

test("the last Voice chosen for each Voice Model is kept in the webview's storage", () => {
  rememberVoice({ modelId: "kokoro:82m", voiceId: "af_bella" });
  rememberVoice({ modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" });
  rememberVoice({ modelId: "kokoro:82m", voiceId: "af_heart" });

  expect(JSON.parse(localStorage.getItem(KEY) ?? "")).toEqual({
    "kokoro:82m": "af_heart",
    "qwen3-tts:0.6b": "Chelsie",
  });
  expect(lastVoiceOf("kokoro:82m")).toBe("af_heart");
  expect(lastVoiceOf("never")).toBeUndefined();
});

test("storage that holds nonsense reads as an empty memory", () => {
  for (const raw of ["{", "null", "[1,2]", "7"]) {
    localStorage.setItem(KEY, raw);
    expect(lastVoiceOf("kokoro:82m")).toBeUndefined();
  }
});

test("a Voice Model is offered its remembered Voice, then the Manifest's pick", () => {
  expect(voiceToOffer(KOKORO)?.id).toBe("af_heart");

  rememberVoice({ modelId: "kokoro:82m", voiceId: "af_bella" });
  expect(voiceToOffer(KOKORO)?.id).toBe("af_bella");

  rememberVoice({ modelId: "kokoro:82m", voiceId: "retired" });
  expect(voiceToOffer(KOKORO)?.id).toBe("af_heart");
});

test("storage that refuses is a memory that holds nothing, not a crash", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      throw new Error("SecurityError");
    },
  });
  try {
    expect(() => rememberVoice({ modelId: "kokoro:82m", voiceId: "af_bella" })).not.toThrow();
    expect(lastVoiceOf("kokoro:82m")).toBeUndefined();
  } finally {
    if (original) Object.defineProperty(globalThis, "localStorage", original);
  }
});
