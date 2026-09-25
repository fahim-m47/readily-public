import { expect, test } from "vitest";
import type { CatalogEntry } from "../engine/client";
import { entriesForMode } from "./mode";

const voice = (id: string, simple: boolean) => ({
  simple,
  id,
  name: id,
  language: "en-US",
  preview: null,
});

const entry = (id: string, voices: CatalogEntry["voices"]): CatalogEntry => ({
  id,
  name: id,
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
  voices,
  defaultVoiceId: voices[0].id,
  downloadBytes: 0,
});

const CATALOG = [
  entry("kokoro:82m", [
    voice("af_heart", true),
    voice("am_puck", false),
    voice("bf_emma", true),
  ]),
  entry("qwen3-tts:0.6b", [voice("Chelsie", false)]),
];

test("Simple keeps the qualified Voices of a model that mixes both kinds", () => {
  const simple = entriesForMode(CATALOG, "simple");
  expect(simple?.map((e) => [e.id, e.voices.map((v) => v.id)])).toEqual([
    ["kokoro:82m", ["af_heart", "bf_emma"]],
  ]);
});

test("Advanced offers the Catalog untouched", () => {
  expect(entriesForMode(CATALOG, "advanced")).toBe(CATALOG);
});

test("a Catalog that has not arrived stays absent in either mode", () => {
  expect(entriesForMode(null, "simple")).toBeNull();
});
