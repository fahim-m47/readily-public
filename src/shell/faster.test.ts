import { expect, test } from "vitest";
import type { Diagnostics } from "../engine/advanced";
import type {
  CatalogEntry,
  HistoryNarration,
  ModelStatus,
  NarrationState,
} from "../engine/client";
import { fasterVoice } from "./faster";

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
  downloadBytes: 353746785,
  runsHere: true,
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
  downloadBytes: 1782579200,
  runsHere: true,
};

const SUPERTONIC: CatalogEntry = {
  ...KOKORO,
  id: "supertonic:99m",
  name: "Supertonic 3",
  voices: [{ simple: true, id: "M3", name: "Male 3", language: "en-US", preview: null }],
  defaultVoiceId: "M3",
};

const CATALOG = [KOKORO, QWEN, SUPERTONIC];

const installed = (...ids: string[]) => {
  const store = new Map<string, ModelStatus>(
    CATALOG.map((entry) => [
      entry.id,
      {
        id: entry.id,
        installed: ids.includes(entry.id),
        diskBytes: ids.includes(entry.id) ? entry.downloadBytes : 0,
        downloadBytes: entry.downloadBytes,
      },
    ]),
  );
  return (entryId: string) => store.get(entryId);
};

const SOURCE = "One. Two. Three.";

const block = (
  ordinal: number,
  sourceStart: number,
  sourceEnd: number,
  timelineStartSec: number | null,
): HistoryNarration["segments"][number] => ({
  ordinal,
  sourceStart,
  sourceEnd,
  boundary: "sentence",
  durationSec: 4,
  audioPresent: timelineStartSec !== null,
  timelineStartSec,
  timings: [],
});

const document = (
  segments = [block(0, 0, 4, 0), block(1, 5, 9, 4)],
  gaps: HistoryNarration["gaps"] = [],
): HistoryNarration => ({
  id: "n-1",
  sourcePreview: SOURCE,
  modelId: QWEN.id,
  voiceId: "Chelsie",
  speed: 1,
  status: "playing",
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
  lastPlayedAt: "2026-01-01T00:00:00Z",
  playheadSec: 0,
  totalDurationSec: null,
  audioPresent: true,
  hasGaps: gaps.length > 0,
  source: SOURCE,
  segments,
  gaps,
});

const ASSEMBLED = document([
  block(0, 0, 4, 0),
  block(1, 5, 9, 4),
  block(2, 10, 16, 9),
]);

const QUIET: Diagnostics = {
  audioSecondsPerSecond: null,
  readySecondsAhead: 0,
  preparingBlock: null,
  generationComplete: false,
  playingBlock: null,
  retries: 0,
  cutoffs: 0,
  ringStarvations: 0,
  deviceUnderflows: 0,
};

const narrating = (
  overrides: Partial<NarrationState> = {},
): NarrationState => ({
  version: 1,
  phase: "preparing",
  narrationId: "n-1",
  modelId: QWEN.id,
  voiceId: "Chelsie",
  positionSec: 0,
  totalSec: 0,
  speed: 1,
  generationBehind: false,
  diagnostics: QUIET,
  level: 0,
  lateCallbacks: 0,
  error: null,
  ...overrides,
});

test("an expressive Narration still waiting on synthesis is offered the instant Tier", () => {
  expect(
    fasterVoice(narrating(), document(), CATALOG, installed(KOKORO.id, QWEN.id)),
  ).toEqual({
    name: "Kokoro",
    selection: { modelId: KOKORO.id, voiceId: "af_heart" },
    source: SOURCE,
  });
});

test("the Manifest's own pick for the escape wins over Catalog order", () => {
  const store = installed(KOKORO.id, QWEN.id, SUPERTONIC.id);
  expect(
    fasterVoice(narrating(), document(), CATALOG, store, SUPERTONIC.id)?.selection,
  ).toEqual({ modelId: SUPERTONIC.id, voiceId: "M3" });
});

test("a preferred escape that is not downloaded gives way to one that is", () => {
  const store = installed(KOKORO.id, QWEN.id);
  expect(
    fasterVoice(narrating(), document(), CATALOG, store, SUPERTONIC.id)?.name,
  ).toBe(KOKORO.name);
});

test("the instant Tier is offered nothing — there is nothing faster to offer", () => {
  const reading = narrating({ modelId: KOKORO.id, voiceId: "af_heart" });

  expect(
    fasterVoice(reading, document(), CATALOG, installed(KOKORO.id, QWEN.id)),
  ).toBe(null);
});

test("a faster Voice Model that is not downloaded is not an escape hatch", () => {
  // An entry the Catalog has and the disk does not answers
  // `409 model_not_installed`.
  expect(
    fasterVoice(narrating(), document(), CATALOG, installed(QWEN.id)),
  ).toBe(null);
});

test("nothing is offered before the model store has answered", () => {
  expect(fasterVoice(narrating(), document(), CATALOG, () => undefined)).toBe(
    null,
  );
});

test("nothing is offered before the Catalog has been read", () => {
  expect(
    fasterVoice(narrating(), document(), null, installed(KOKORO.id, QWEN.id)),
  ).toBe(null);
});

test("a Voice Model the Catalog does not rank is neither offered nor overtaken", () => {
  const unranked: CatalogEntry = { ...KOKORO, id: "mystery:1", tier: "velvet" };
  const catalog = [unranked, KOKORO, QWEN];
  const store = installed(unranked.id, KOKORO.id, QWEN.id);

  expect(fasterVoice(narrating(), document(), catalog, store)?.name).toBe(
    "Kokoro",
  );
  expect(
    fasterVoice(narrating({ modelId: unranked.id }), document(), catalog, store),
  ).toBe(null);
});

test("a Narration playing on a thin buffer is still waiting on synthesis", () => {
  const reading = narrating({ phase: "playing", positionSec: 8, totalSec: 12 });

  expect(
    fasterVoice(reading, document(), CATALOG, installed(KOKORO.id, QWEN.id))
      ?.name,
  ).toBe("Kokoro");
});

test("the offer goes once synthesis is comfortably ahead of playback", () => {
  const reading = narrating({ phase: "playing", positionSec: 8, totalSec: 90 });

  expect(
    fasterVoice(reading, document(), CATALOG, installed(KOKORO.id, QWEN.id)),
  ).toBe(null);
});

test("the last seconds of an assembled Narration are not a wait", () => {
  const ending = narrating({ phase: "playing", positionSec: 88, totalSec: 90 });
  const short = narrating({ phase: "playing", positionSec: 0, totalSec: 9 });
  const store = installed(KOKORO.id, QWEN.id);

  expect(fasterVoice(ending, ASSEMBLED, CATALOG, store)).toBe(null);
  expect(fasterVoice(short, ASSEMBLED, CATALOG, store)).toBe(null);
});

test("seeking back through audio that already exists is not a wait either", () => {
  // A seek re-enters `preparing` while the Engine locates the target.
  const seeking = narrating({ phase: "preparing", positionSec: 4 });

  expect(
    fasterVoice(seeking, ASSEMBLED, CATALOG, installed(KOKORO.id, QWEN.id)),
  ).toBe(null);
});

test("a Block the Engine gave up on is text it has finished with", () => {
  const withGap = document(
    [block(0, 0, 4, 0), block(1, 5, 9, null), block(2, 10, 16, 4)],
    [
      {
        ordinal: 1,
        sourceStart: 5,
        sourceEnd: 9,
        errorCode: "synthesis_failed",
        createdAt: "2026-01-01T00:00:00Z",
      },
    ],
  );
  const ending = narrating({ phase: "playing", positionSec: 8, totalSec: 10 });

  expect(
    fasterVoice(ending, withGap, CATALOG, installed(KOKORO.id, QWEN.id)),
  ).toBe(null);
});

test("a document that is some other Narration is nothing to restart from", () => {
  const other = { ...document(), id: "n-2" };

  expect(
    fasterVoice(narrating(), other, CATALOG, installed(KOKORO.id, QWEN.id)),
  ).toBe(null);
  expect(
    fasterVoice(narrating(), null, CATALOG, installed(KOKORO.id, QWEN.id)),
  ).toBe(null);
});

test("a Narration nobody is waiting on is offered nothing", () => {
  const store = installed(KOKORO.id, QWEN.id);
  const phases = ["idle", "paused", "finished", "failed"] as const;

  for (const phase of phases) {
    expect(fasterVoice(narrating({ phase }), document(), CATALOG, store)).toBe(
      null,
    );
  }
  expect(fasterVoice(null, document(), CATALOG, store)).toBe(null);
});

test("a fully assembled Source with an emoji in it is still not a wait", () => {
  // The Engine's `sourceEnd` is a Python string index — code points — and
  // the owl is one code point but two UTF-16 code units.
  const source = "🦉 One. Two.";
  const assembled = {
    ...document([block(0, 0, 6, 0), block(1, 7, 11, 4)]),
    sourcePreview: source,
    source,
  };
  const ending = narrating({ phase: "playing", positionSec: 8, totalSec: 10 });

  expect(
    fasterVoice(ending, assembled, CATALOG, installed(KOKORO.id, QWEN.id)),
  ).toBe(null);
});
