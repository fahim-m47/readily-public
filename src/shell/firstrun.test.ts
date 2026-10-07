import { expect, test } from "vitest";
import type { CatalogEntry, Connection, DownloadState } from "../engine/client";
import type { FirstRunFacts } from "./firstrun";
import { firstRunStep } from "./firstrun";
import { licencesOf } from "./terms";
import manifest from "../../catalog/manifest.json";

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

const READY: Connection = { state: "ready" };

const facts = (over: Partial<FirstRunFacts> = {}): FirstRunFacts => ({
  connection: READY,
  anyVoiceModel: false,
  firstVoiceModel: KOKORO,
  storeFailure: null,
  unaccepted: [],
  download: null,
  downloadFailure: null,
  ...over,
});

const download = (state: Partial<DownloadState>): DownloadState => ({
  version: 1,
  phase: "idle",
  modelId: KOKORO.id,
  bytesTotal: KOKORO.downloadBytes,
  bytesDownloaded: 0,
  error: null,
  queue: [],
  failures: [],
  ...state,
});

test("the first-run screen says what the multi-minute wait is for", () => {
  const step = firstRunStep(
    facts({
      connection: { state: "starting", detail: "provisioning", note: null },
      anyVoiceModel: null,
      firstVoiceModel: null,
    }),
  );

  expect(step).toMatchObject({ kind: "provisioning", tone: "working", retry: null });
  expect(step?.message).toBe("Setting up Readily…");
  expect(step?.detail).toMatch(/once/);
});

test("the environment build shows uv's own line, so a long wait proves it is moving", () => {
  const step = firstRunStep(
    facts({
      connection: {
        state: "starting",
        detail: "provisioning",
        note: "Downloading torch (619.6MiB)",
      },
      anyVoiceModel: null,
      firstVoiceModel: null,
    }),
  );

  expect(step?.aside).toBe("Downloading torch (619.6MiB)");
  expect(step?.progress).toBeNull();
  expect(step?.message).toBe("Setting up Readily…");
});

test("an ordinary launch waits on the Engine by name", () => {
  const launching = firstRunStep(
    facts({ connection: { state: "starting", detail: "launching" } }),
  );
  const restarting = firstRunStep(
    facts({ connection: { state: "starting", detail: "restarting" } }),
  );

  expect(launching?.kind).toBe("starting");
  expect(launching?.message).toBe("Starting the Engine…");
  expect(restarting?.message).toBe("Restarting the Engine…");
});

test("an Engine that gave up leads with plain language and keeps its own words", () => {
  const step = firstRunStep(
    facts({
      connection: {
        state: "failed",
        message: "`uv sync --locked` failed — gave up after 5 attempts",
      },
      anyVoiceModel: null,
    }),
  );

  expect(step).toMatchObject({
    kind: "engine-failed",
    tone: "failed",
    retry: "engine",
    progress: null,
  });
  expect(step?.message).toBe("Readily could not finish setting itself up.");
  expect(step?.detail).toMatch(/network/i);
  expect(step?.aside).toBe("`uv sync --locked` failed — gave up after 5 attempts");
});

test("licences not yet accepted are asked about before anything is fetched or shown", () => {
  const unaccepted = licencesOf([KOKORO]);

  expect(firstRunStep(facts({ unaccepted }))).toMatchObject({ kind: "terms", tone: "asking" });
  expect(firstRunStep(facts({ unaccepted, anyVoiceModel: true }))?.kind).toBe("terms");
});

test("the terms wait on the Catalog, and a Catalog that refused does not lock out a model on disk", () => {
  const refused = { unaccepted: null, storeFailure: "The Catalog could not be read." };

  expect(firstRunStep(facts({ unaccepted: null, anyVoiceModel: true }))?.kind).toBe("waiting");
  expect(firstRunStep(facts({ ...refused, anyVoiceModel: true }))).toBeNull();
  expect(firstRunStep(facts({ ...refused, anyVoiceModel: false }))).toMatchObject({
    kind: "store-failed",
    retry: "store",
  });
});

test("a ready Engine with a Voice Model on disk hands the app over", () => {
  expect(firstRunStep(facts({ anyVoiceModel: true }))).toBeNull();
  expect(
    firstRunStep(
      facts({ anyVoiceModel: true, download: download({ phase: "downloading" }) }),
    ),
  ).toBeNull();
  expect(
    firstRunStep(facts({ anyVoiceModel: true, storeFailure: "Disk is busy." })),
  ).toBeNull();
});

test("a store that has not answered yet is a wait, not an empty disk", () => {
  const step = firstRunStep(facts({ anyVoiceModel: null }));

  expect(step?.kind).toBe("waiting");
});

test("a Catalog that has not arrived yet is a wait too", () => {
  expect(firstRunStep(facts({ firstVoiceModel: null }))?.kind).toBe("waiting");
});

test("a store that refused is a button, not a spinner that never moves", () => {
  const step = firstRunStep(
    facts({
      anyVoiceModel: null,
      firstVoiceModel: null,
      storeFailure: "The Catalog could not be read.",
    }),
  );

  expect(step).toMatchObject({ kind: "store-failed", tone: "failed", retry: "store" });
  expect(step?.aside).toBe("The Catalog could not be read.");
});

test("an empty disk names the Voice Model being fetched and what it costs", () => {
  const step = firstRunStep(facts());

  expect(step).toMatchObject({
    kind: "getting-voice-model",
    tone: "working",
    progress: null,
    retry: null,
  });
  expect(step?.message).toBe("Getting the first voice model…");
  expect(step?.detail).toBe("Kokoro · 337.4 MB to download");
});

// The size on the first-run screen is whatever the Catalog says the model a
// first run installs costs, and nothing else: the fixture above carries the
// committed manifest's own sum for Kokoro, so when those files change this
// test and the figure readers see move together, and a number someone typed
// cannot.
test("the first voice model's size is the Catalog's, not a number someone typed", () => {
  const first = manifest.models.find((model) => model.name === "kokoro");
  const bytes = first?.files.reduce((sum, file) => sum + file.size_bytes, 0);
  expect(bytes).toBe(KOKORO.downloadBytes);
});

test("a download that never started is a button, not a wait for a snapshot", () => {
  const step = firstRunStep(
    facts({ downloadFailure: "That download could not be started." }),
  );

  expect(step).toMatchObject({
    kind: "download-failed",
    tone: "failed",
    retry: "download",
  });
  expect(step?.message).toBe("That download could not be started.");
});

test("a download in flight is bytes against bytes, and a bar to draw", () => {
  const step = firstRunStep(
    facts({
      download: download({ phase: "downloading", bytesDownloaded: 120_000_000 }),
    }),
  );

  expect(step).toMatchObject({
    kind: "downloading",
    tone: "working",
    retry: null,
    progress: { done: 120_000_000, total: KOKORO.downloadBytes },
  });
  expect(step?.message).toBe("Getting the first voice model…");
  expect(step?.detail).toBe("Kokoro — 114.4 MB of 337.4 MB");
});

test("verification is its own beat, with no bar to sit at 100%", () => {
  const step = firstRunStep(
    facts({
      download: download({
        phase: "verifying",
        bytesDownloaded: KOKORO.downloadBytes,
      }),
    }),
  );

  expect(step).toMatchObject({ kind: "verifying", progress: null, retry: null });
  expect(step?.detail).toBe("Checking the files match what Readily expects…");
});

test("a network failure promises a resume; a bad hash promises a fresh start", () => {
  const dropped = firstRunStep(
    facts({
      download: download({
        phase: "failed",
        bytesDownloaded: 120_000_000,
        error: {
          version: 1,
          code: "download_failed",
          message: "The network dropped.",
        },
      }),
    }),
  );
  const corrupt = firstRunStep(
    facts({
      download: download({
        phase: "failed",
        error: {
          version: 1,
          code: "verification_failed",
          message: "The files did not match.",
        },
      }),
    }),
  );

  expect(dropped).toMatchObject({
    kind: "download-failed",
    tone: "failed",
    retry: "download",
    progress: null,
  });
  expect(dropped?.message).toBe("The network dropped.");
  expect(dropped?.detail).toBe("Trying again picks up where it stopped.");
  expect(corrupt?.detail).toBe("Trying again downloads it fresh.");
});

test("a download that finished waits for the store rather than claiming success", () => {
  const step = firstRunStep(
    facts({
      download: download({
        phase: "installed",
        bytesDownloaded: KOKORO.downloadBytes,
      }),
    }),
  );

  expect(step?.kind).toBe("waiting");
  expect(step?.message).toBe("Nearly ready…");
});

test("a download of some other model is not this screen's download", () => {
  const step = firstRunStep(
    facts({
      download: download({ phase: "downloading", modelId: "qwen3-tts:0.6b" }),
    }),
  );

  expect(step?.kind).toBe("getting-voice-model");
});
