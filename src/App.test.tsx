import {
  act,
  cleanup,
  createEvent,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";
import type { Diagnostics } from "./engine/advanced";
import type {
  Catalog,
  Connection,
  DownloadHandlers,
  DownloadState,
  EngineClient,
  HistoryEntry,
  HistoryNarration,
  ModelStatus,
  NarrationState,
  RetentionSettings,
  UpdateStatus,
  VoiceSelection,
  WatchHandlers,
  WireError,
} from "./engine/client";
import { SOURCE_CHARACTER_LIMIT } from "./shell/estimate";
import { acceptLicences } from "./shell/terms";


// The Engine's sentence, wherever the shell put it — or nothing at all when
// it is nowhere. For a test that only cares that the reader was told.
const engineSaid = (sentence: string) => screen.queryAllByText(sentence)[0] ?? null;

const modeSelect = () => screen.getByRole("button", { name: /^Reading mode:/ });
const chooseAdvanced = () => {
  fireEvent.click(modeSelect());
  fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
};

// Renders the app and waits past the launch screen to the shell, the way a
// reader with the Engine up and a Voice Model on disk arrives at it. Shell
// scenarios run in Advanced, where every control shows; the tests about
// Simple render the app themselves.
const renderShell = async (client: EngineClient) => {
  render(<App client={client} />);
  await screen.findByRole("main", { name: "Readily" });
  // A Narration already running holds the mode where it is.
  const mode = screen.getByRole("button", { name: /^Reading mode:/ });
  if (mode.getAttribute("aria-label") === "Reading mode: Simple" && !mode.hasAttribute("disabled")) {
    chooseAdvanced();
  }
};

// Every fixture's licence, accepted the way a returning reader already has;
// the tests about the launch screen's tick clear it.
beforeEach(() => acceptLicences(["Apache-2.0"]));

afterEach(() => {
  cleanup();
  localStorage.clear();
});

test("the Settings sheet no longer edits prepare-first; the Advanced panel shows it on", async () => {
  const { client } = fakeClient({ catalog: PREPARE_FIRST_CATALOG });
  await renderShell(client);
  const field = await screen.findByRole("checkbox", { name: "Prepare first" });
  expect(field).toHaveProperty("checked", true);

  fireEvent.click(modeSelect());
  fireEvent.click(screen.getByRole("button", { name: "Simple" }));
  const settings = await openSettings();
  expect(within(settings).queryByRole("checkbox", { name: /Prepare/ })).toBeNull();
});

test("Simple offers 4× and the player explains falling behind without changing speed", async () => {
  const { client, setSpeed, emit } = fakeClient({ snapshots: [{ ...IDLE, phase: "finished", narrationId: "n-1", generationBehind: true }] });
  render(<App client={client} />);
  const speed = await screen.findByRole("combobox", { name: "Playback speed" });
  expect(modeSelect().getAttribute("aria-label")).toBe("Reading mode: Simple");
  fireEvent.change(speed, { target: { value: "4" } });
  expect(setSpeed).toHaveBeenCalledWith(4);
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", speed: 4, generationBehind: true });
  expect(screen.getByText(/Generation is falling behind/)).toBeTruthy();
  expect(setSpeed).toHaveBeenCalledTimes(1);
  expect(modeSelect().getAttribute("aria-label")).toBe("Reading mode: Simple");
  expect(
    modeSelect(),
  ).toHaveProperty("disabled", true);
});

test("a reconnect does not drop the reader back into Simple mode", async () => {
  const { client, connect } = fakeClient();
  render(<App client={client} />);
  await screen.findByRole("button", { name: "Reading mode: Simple" });
  chooseAdvanced();
  expect(modeSelect().getAttribute("aria-label")).toBe("Reading mode: Advanced");

  connect({ state: "failed", message: "The Engine disconnected." });
  connect({ state: "ready" });

  expect(modeSelect().getAttribute("aria-label")).toBe("Reading mode: Advanced");
});

test("a Catalog that has not been read is not grounds to withhold Narrate in Simple", async () => {
  const { client, narrate } = fakeClient({ catalogFails: true });
  render(<App client={client} />);
  await screen.findByRole("main", { name: "Readily" });

  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "A local Narration." },
  });
  await waitFor(() => expect(narrateButton().disabled).toBe(false));
  fireEvent.click(narrateButton());

  await waitFor(() => expect(narrate).toHaveBeenCalledWith("A local Narration.", {
    modelId: "kokoro:82m", voiceId: "af_heart",
  }, "simple"));
});

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

const IDLE: NarrationState = {
  version: 1,
  phase: "idle",
  narrationId: null,
  modelId: "kokoro:82m",
  voiceId: "af_heart",
  positionSec: 0,
  totalSec: 0,
  speed: 1,
  generationBehind: false,
  diagnostics: QUIET,
  level: 0,
  lateCallbacks: 0,
  error: null,
};

const ENTRY: HistoryEntry = {
  id: "n-1",
  sourcePreview: "The sea was calm.",
  modelId: "kokoro:82m",
  voiceId: "af_heart",
  speed: 1,
  status: "finished",
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  lastPlayedAt: new Date().toISOString(),
  playheadSec: 0,
  totalDurationSec: 62,
  audioPresent: true,
  hasGaps: false,
};

const KOKORO_BYTES = 1024 * 1024 * 337;

const CATALOG: Catalog = {
  defaultModelId: "kokoro:82m",
  models: [
    {
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
        {
          simple: true,
          id: "af_heart",
          name: "Heart",
          language: "en-US",
          preview: "kokoro/82m/af_heart.m4a",
        },
      ],
      defaultVoiceId: "af_heart",
      downloadBytes: KOKORO_BYTES,
      runsHere: true,
    },
    {
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
      voices: [
        { simple: false, id: "Chelsie", name: "Chelsie", language: "en-US", preview: null },
      ],
      defaultVoiceId: "Chelsie",
      downloadBytes: 1024 * 1024 * 1700,
      runsHere: true,
    },
  ],
};

const PREPARE_FIRST_CATALOG: Catalog = {
  ...CATALOG,
  models: CATALOG.models.map((entry) =>
    entry.id === "kokoro:82m"
      ? {
          ...entry,
          parameters: {
            prepare_first: {
              label: "Prepare first",
              kind: "boolean" as const,
              unit: "on/off",
              description: "Prepare all audio before listening",
            },
          },
        }
      : entry,
  ),
};

const QWEN_BYTES = 1024 * 1024 * 1700;

const NOTHING_INSTALLED: ModelStatus[] = [
  {
    id: "kokoro:82m",
    installed: false,
    diskBytes: 0,
    downloadBytes: KOKORO_BYTES,
  },
  {
    id: "qwen3-tts:0.6b",
    installed: false,
    diskBytes: 0,
    downloadBytes: QWEN_BYTES,
  },
];

const BOTH_INSTALLED: ModelStatus[] = [
  {
    id: "kokoro:82m",
    installed: true,
    diskBytes: KOKORO_BYTES,
    downloadBytes: KOKORO_BYTES,
  },
  {
    id: "qwen3-tts:0.6b",
    installed: true,
    diskBytes: QWEN_BYTES,
    downloadBytes: QWEN_BYTES,
  },
];

const downloadingKokoro = (
  overrides: Partial<DownloadState> = {},
): DownloadState => ({
  version: 1,
  phase: "downloading",
  modelId: "kokoro:82m",
  bytesTotal: KOKORO_BYTES,
  bytesDownloaded: 1024 * 1024 * 120,
  error: null,
  queue: [],
  failures: [],
  ...overrides,
});

// Kokoro's download once it has failed: the error of the job that just
// ran, and the failure the Engine keeps for the row.
const failedKokoro = (): DownloadState => {
  const error: WireError = {
    version: 1,
    code: "download_failed",
    message: "The download could not be completed.",
  };
  return downloadingKokoro({
    phase: "failed",
    error,
    failures: [{ modelId: "kokoro:82m", error }],
  });
};

const detailOf = (
  entry: HistoryEntry,
  source: string,
  gaps: HistoryNarration["gaps"] = [],
  segments: HistoryNarration["segments"] = [],
): HistoryNarration => ({ ...entry, source, segments, gaps });

const segment = (
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
  audioPresent: true,
  timelineStartSec,
  timings: [],
});

const THREE_BLOCKS = detailOf(ENTRY, "One. Two. Three.", [], [
  segment(0, 0, 4, 0),
  segment(1, 5, 9, 4),
  segment(2, 10, 16, 9),
]);

const GIGABYTE = 1024 * 1024 * 1024;

// ADR 0004 §5's own defaults.
const DEFAULT_RETENTION: RetentionSettings = {
  segmentBudgetBytes: 5 * GIGABYTE,
  keepAudioDays: null,
  diskUsage: {
    modelsBytes: KOKORO_BYTES + QWEN_BYTES,
    audioBytes: 3 * GIGABYTE,
  },
};

const fakeClient = ({
  connection = { state: "ready" } as Connection,
  snapshots = [] as NarrationState[],
  history = [] as HistoryEntry[],
  detail = detailOf(ENTRY, "The sea was calm."),
  openNarration = vi.fn(async (narrationId: string) =>
    narrationId === detail.id ? detail : { ...detail, id: narrationId },
  ),
  narrate = vi.fn(async () => {}),
  stop = vi.fn(async () => {}),
  pause = vi.fn(async () => {}),
  play = vi.fn(async () => {}),
  seek = vi.fn(async (positionSec: number) => void positionSec),
  seekTime = vi.fn(async (positionSec: number) => void positionSec),
  setSpeed = vi.fn(async (speed: number) => void speed),
  exportNarration = vi.fn(async () => {}),
  fetchPage = vi.fn(async (url: string) => ({
    bytes: new TextEncoder().encode(`The page at ${url}.`),
    contentType: "text/plain",
  })),
  resumeNarration = vi.fn(async () => {}),
  deleteNarration = vi.fn(async () => ({
    narrationId: "n-1",
    audioBytesFreed: 0,
  })),
  catalog = CATALOG,
  catalogFails = false,
  models = BOTH_INSTALLED,
  holdModels = false,
  downloadModel = vi.fn(async () => {}),
  deleteModel = vi.fn<EngineClient["deleteModel"]>(async () => ({ queued: false })),
  withdrawModel = vi.fn<EngineClient["withdrawModel"]>(async () => {}),
  selection = { modelId: "kokoro:82m", voiceId: "af_heart" } as VoiceSelection,
  voiceFails = false,
  selectVoice = vi.fn(async (chosen: VoiceSelection) => chosen),
  retryEngine = vi.fn(async () => {}),
  retention = DEFAULT_RETENTION,
  retentionFails = false,
  holdRetention = false,
  openAudioFolder = vi.fn(async () => {}),
  openDataFolder = vi.fn(async () => {}),
  revealLogs = vi.fn(async () => {}),
  update = { state: "idle" } as UpdateStatus,
  installUpdate = vi.fn(async () => {}),
  openDownloadPage = vi.fn(async () => {}),
} = {}) => {
  let offering = update;
  const updateStatus = vi.fn(async () => offering);
  let listening: WatchHandlers = {};
  let watching: DownloadHandlers = {};
  let listing = history;
  let store = models;
  const watch = vi.fn(async (handlers: WatchHandlers) => {
    listening = handlers;
    handlers.onConnection?.(connection);
    for (const snapshot of snapshots) handlers.onNarration?.(snapshot);
  });
  const listHistory = vi.fn(async () => listing);
  let catalogRefuses = catalogFails;
  const listCatalog = vi.fn(async () => {
    if (catalogRefuses) throw new Error("The Catalog could not be read.");
    return catalog;
  });
  let release = () => {};
  const held = holdModels
    ? new Promise<void>((resolve) => {
        release = resolve;
      })
    : Promise.resolve();
  const listModels = vi.fn(async () => {
    await held;
    return store;
  });
  const watchDownloads = vi.fn(async (handlers: DownloadHandlers) => {
    watching = handlers;
  });
  let chosenVoice = selection;
  const voiceSelection = vi.fn(async () => {
    if (voiceFails) throw new Error("The voice settings table is missing.");
    return chosenVoice;
  });
  const storeVoice = vi.fn(async (chosen: VoiceSelection) => {
    chosenVoice = await selectVoice(chosen);
    return chosenVoice;
  });
  let policy = retention;
  let readFails = false;
  const readRetention = vi.fn(async () => {
    if (readFails) throw new Error("Retention could not be read.");
    return policy;
  });
  let answerRetention = () => {};
  const heldRetention = holdRetention
    ? new Promise<void>((resolve) => {
        answerRetention = resolve;
      })
    : Promise.resolve();
  const setRetention = vi.fn(
    async (chosen: { segmentBudgetBytes: number; keepAudioDays: number | null }) => {
      await heldRetention;
      if (retentionFails) throw new Error("Retention could not be saved.");
      const audioBytes = Math.min(
        policy.diskUsage.audioBytes,
        chosen.segmentBudgetBytes,
      );
      const evictedBytes = policy.diskUsage.audioBytes - audioBytes;
      policy = {
        ...chosen,
        diskUsage: { ...policy.diskUsage, audioBytes },
      };
      return { ...policy, evictedBytes };
    },
  );
  const client = {
    watch,
    narrate,
    stop,
    pause,
    play,
    seek,
    seekTime,
    setSpeed,
    controls: async () => ({ overrides: {}, effectiveValues: { prepare_first: true } }),
    setControls: async (_voice, overrides) => ({ overrides, effectiveValues: overrides }),
    selectTake: async () => {},
    listHistory,
    openNarration,
    resumeNarration,
    deleteNarration,
    exportNarration,
    fetchPage,
    listCatalog,
    listModels,
    downloadModel,
    deleteModel,
    withdrawModel,
    watchDownloads,
    voiceSelection,
    selectVoice: storeVoice,
    retryEngine,
    retention: readRetention,
    setRetention,
    openAudioFolder,
    openDataFolder,
    revealLogs,
    updateStatus,
    installUpdate,
    openDownloadPage,
  } satisfies EngineClient;

  return {
    client,
    narrate,
    stop,
    pause,
    play,
    seek,
    seekTime,
    setSpeed,
    listHistory,
    openNarration,
    resumeNarration,
    deleteNarration,
    exportNarration,
    fetchPage,
    listCatalog,
    listModels,
    downloadModel,
    deleteModel,
    withdrawModel,
    voiceSelection,
    selectVoice,
    retryEngine,
    readRetention,
    setRetention,
    openAudioFolder,
    openDataFolder,
    revealLogs,
    updateStatus,
    installUpdate,
    // What the supervisor answers the next poll with. The check happens in
    // Rust seconds after launch, so a test that wants the offer moves this
    // and lets the poll find it.
    serveUpdate: (next: UpdateStatus) => {
      offering = next;
    },
    serveRetention: (next: RetentionSettings) => {
      policy = next;
    },
    failReads: (failing: boolean) => {
      readFails = failing;
    },
    answerRetention: () => act(async () => answerRetention()),
    recoverCatalog: () => {
      catalogRefuses = false;
    },
    serveModels: (next: ModelStatus[]) => {
      store = next;
    },
    releaseModels: () => act(async () => release()),
    emitDownload: (snapshot: DownloadState) =>
      act(() => watching.onDownload?.(snapshot)),
    loseDownloads: () => act(() => watching.onLost?.()),
    serve: (next: HistoryEntry[]) => {
      listing = next;
    },
    emit: (snapshot: NarrationState) =>
      act(() => listening.onNarration?.(snapshot)),
    connect: (next: Connection) => act(() => listening.onConnection?.(next)),
  };
};

const historyList = () => screen.getByRole("group", { name: "History" });
const historyRows = () => within(historyList()).queryAllByRole("listitem");
const openRow = (index = 0) =>
  within(historyRows()[index]).getAllByRole("button")[0];

const narrateButton = () =>
  screen.getByRole("button", { name: "Narrate" }) as HTMLButtonElement;

const emptyDisk = async (overrides: Parameters<typeof fakeClient>[0] = {}) => {
  const fake = fakeClient({ models: BOTH_INSTALLED, ...overrides });
  await renderShell(fake.client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());

  fake.serveModels(NOTHING_INSTALLED);
  await fake.emitDownload(
    downloadingKokoro({ phase: "idle", bytesDownloaded: 0 }),
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /^Voice Model/ })).toBeTruthy(),
  );
  await waitFor(() => expect(fake.listModels).toHaveBeenCalledTimes(2));
  return fake;
};

test("the page keeps its h1 even once a Narration is running", async () => {
  const { client } = fakeClient({ snapshots: [{ ...IDLE, phase: "playing" }] });
  await renderShell(client);
  await waitFor(() => expect(engineSaid("Reading aloud…")).toBeTruthy());

  expect(
    screen.getByRole("heading", { level: 1, name: "What should I read?" }),
  ).toBeTruthy();
});

test("the composer counts a Source the way the Engine counts it", async () => {
  // The Engine caps `input` in code points; an emoji is one character to it
  // and two UTF-16 code units to JavaScript.
  const { client } = fakeClient();
  await renderShell(client);

  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "🌊".repeat(3) },
  });

  await waitFor(() =>
    expect(screen.getByText(/^3 characters/)).toBeTruthy(),
  );
});

test("Open link reads the page through the Engine into the draft", async () => {
  const { client, fetchPage } = fakeClient();
  await renderShell(client);

  fireEvent.click(screen.getByRole("button", { name: "Open link" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Link" }), {
    target: { value: "https://example.com/story" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Read" }));

  await waitFor(() =>
    expect(
      (screen.getByRole("textbox", { name: "Source" }) as HTMLTextAreaElement).value,
    ).toBe("The page at https://example.com/story."),
  );
  expect(fetchPage).toHaveBeenCalledWith("https://example.com/story");
});

test("past its first run, Readily opens on an empty shell and nothing else", async () => {
  const { client } = fakeClient({ models: BOTH_INSTALLED });
  await renderShell(client);

  expect(screen.getByRole("complementary", { name: "Sidebar" })).toBeTruthy();
  expect(screen.getByRole("main", { name: "Readily" })).toBeTruthy();
  expect(screen.getByRole("heading", { level: 1, name: "What should I read?" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Source" })).toBeTruthy();
  expect(narrateButton().disabled).toBe(true);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  // With no player to carry it, the row is where a sentence would be shown.
  expect(
    document.querySelector(".narration__line")?.classList.contains("visually-hidden"),
  ).toBe(false);
});

test("the launch screen covers the composer until the Engine is ready", async () => {
  const { client, connect } = fakeClient({
    connection: { state: "starting", detail: "restarting" },
  });
  render(<App client={client} />);

  expect(await screen.findByText("Restarting the Engine…")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Source" })).toBe(null);

  await connect({ state: "starting", detail: "launching" });
  expect(screen.getByText("Starting the Engine…")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Source" })).toBe(null);

  await connect({ state: "ready" });
  expect(await screen.findByRole("textbox", { name: "Source" })).toBeTruthy();
});

// ~337 MB to fetch before Readily can read a word (ADR 0001 §6).

test("a clean machine is told what the wait is for, not shown an app that cannot read", async () => {
  const { client } = fakeClient({
    connection: { state: "starting", detail: "provisioning", note: null },
  });
  render(<App client={client} />);

  await waitFor(() =>
    expect(screen.getByText("Setting up Readily…")).toBeTruthy(),
  );
  expect(screen.getByText(/This happens once/)).toBeTruthy();
  expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBe(null);
  expect(screen.queryByRole("textbox", { name: "Source" })).toBe(null);
});

test("one tick accepts every licence before anything is downloaded, and is remembered", async () => {
  localStorage.clear();
  const { client, downloadModel } = fakeClient({ models: NOTHING_INSTALLED });
  render(<App client={client} />);

  const tick = await screen.findByRole("checkbox", {
    name: "I accept the terms and conditions of every voice model Readily supports",
  });
  fireEvent.click(screen.getByRole("button", { name: "Apache 2.0" }));
  expect(await screen.findByText(/Apache License/)).toBeTruthy();
  expect(downloadModel).not.toHaveBeenCalled();

  fireEvent.click(tick);
  await waitFor(() => expect(downloadModel).toHaveBeenCalledWith("kokoro:82m"));

  cleanup();
  const again = fakeClient();
  await renderShell(again.client);
  expect(screen.queryByRole("checkbox")).toBe(null);
});

test("a Catalog that recovers after launch asks for the licences before anything can be downloaded", async () => {
  localStorage.clear();
  const { client, recoverCatalog, emitDownload } = fakeClient({ catalogFails: true });
  render(<App client={client} />);
  await screen.findByRole("main", { name: "Readily" });

  recoverCatalog();
  await emitDownload(downloadingKokoro({ phase: "idle", bytesDownloaded: 0 }));

  fireEvent.click(await screen.findByRole("checkbox", {
    name: "I accept the terms and conditions of every voice model Readily supports",
  }));
  expect(await screen.findByRole("main", { name: "Readily" })).toBeTruthy();
});

test("the first Voice Model is fetched without being asked for, and named while it lands", async () => {
  const { client, downloadModel, emitDownload, serveModels } = fakeClient({
    models: NOTHING_INSTALLED,
  });
  render(<App client={client} />);

  await waitFor(() =>
    expect(downloadModel).toHaveBeenCalledWith("kokoro:82m"),
  );
  await emitDownload(downloadingKokoro());
  expect(screen.getByText("Getting the first voice model…")).toBeTruthy();
  expect(screen.getByText("Kokoro — 120.0 MB of 337.0 MB")).toBeTruthy();
  expect(screen.getByRole("progressbar", { name: "Download progress" })).toBeTruthy();

  await emitDownload(downloadingKokoro({ phase: "verifying" }));
  expect(
    screen.getByText("Checking the files match what Readily expects…"),
  ).toBeTruthy();

  serveModels(BOTH_INSTALLED);
  await emitDownload(
    downloadingKokoro({ phase: "installed", bytesDownloaded: KOKORO_BYTES }),
  );

  await waitFor(() =>
    expect(
      screen.getByRole("heading", { level: 1, name: "What should I read?" }),
    ).toBeTruthy(),
  );
  expect(downloadModel).toHaveBeenCalledTimes(1);
});

test("a first-run download that dies is a retry, not a dead end", async () => {
  const { client, downloadModel, emitDownload } = fakeClient({
    models: NOTHING_INSTALLED,
  });
  render(<App client={client} />);
  await waitFor(() => expect(downloadModel).toHaveBeenCalledTimes(1));

  await emitDownload(
    downloadingKokoro({
      phase: "failed",
      error: {
        version: 1,
        code: "download_failed",
        message: "The download could not be completed.",
      },
    }),
  );

  expect(
    screen.getByText("The download could not be completed."),
  ).toBeTruthy();
  expect(
    screen.getByText("Trying again picks up where it stopped."),
  ).toBeTruthy();
  expect(downloadModel).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("button", { name: "Try again" }));

  expect(downloadModel).toHaveBeenCalledTimes(2);
});

test("an Engine that gave up on a first run can be started over", async () => {
  const { client, retryEngine } = fakeClient({
    connection: {
      state: "failed",
      message: "`uv sync --locked` failed — gave up after 5 attempts",
    },
    models: NOTHING_INSTALLED,
  });
  render(<App client={client} />);

  await waitFor(() =>
    expect(
      screen.getByText("Readily could not finish setting itself up."),
    ).toBeTruthy(),
  );
  expect(screen.getByText(/Setting up needs the network/)).toBeTruthy();
  expect(
    screen.getByText("`uv sync --locked` failed — gave up after 5 attempts"),
  ).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Try again" }));

  await waitFor(() => expect(retryEngine).toHaveBeenCalledOnce());
});

test("a first run whose reads refuse is a button, not a spinner forever", async () => {
  const { client, listCatalog } = fakeClient({
    catalogFails: true,
    models: NOTHING_INSTALLED,
  });
  render(<App client={client} />);

  await waitFor(() =>
    expect(
      screen.getByText("Readily could not check what is already downloaded."),
    ).toBeTruthy(),
  );
  expect(screen.getByText("The Catalog could not be read.")).toBeTruthy();
  expect(listCatalog).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("button", { name: "Try again" }));

  await waitFor(() => expect(listCatalog).toHaveBeenCalledTimes(2));
});

test("the first-run screen never comes back once the app has been usable", async () => {
  await emptyDisk();

  expect(
    screen.getByRole("heading", { level: 1, name: "What should I read?" }),
  ).toBeTruthy();
  expect(screen.queryByText("Getting the first voice model…")).toBe(null);
});

test("the composer estimates the Source before anything is narrated", async () => {
  const { client } = fakeClient();
  await renderShell(client);

  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "a".repeat(155) },
  });

  await waitFor(() =>
    expect(screen.getByText("155 characters · about 0:10")).toBeTruthy(),
  );
});

test("a file let go outside the composer is refused; dragged text is not", () => {
  const { client } = fakeClient();
  render(<App client={client} />);

  const file = createEvent.drop(document.body, {
    dataTransfer: { types: ["Files"], files: [], items: [] },
  });
  fireEvent(document.body, file);
  expect(file.defaultPrevented).toBe(true);

  const text = createEvent.dragOver(document.body, {
    dataTransfer: { types: ["text/plain"] },
  });
  fireEvent(document.body, text);
  expect(text.defaultPrevented).toBe(false);
});

test("Narrate sends the Source, and the Voice the pill is showing", async () => {
  const { client, narrate } = fakeClient({
    selection: { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" },
  });
  await renderShell(client);
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "A local Narration." },
  });
  await waitFor(() => expect(narrateButton().disabled).toBe(false));
  await voiceShown("Qwen3 TTS", "Chelsie");

  fireEvent.click(narrateButton());

  await waitFor(() =>
    expect(narrate).toHaveBeenCalledWith("A local Narration.", {
      modelId: "qwen3-tts:0.6b",
      voiceId: "Chelsie",
    }, "advanced"),
  );
});

test("a Source past the Engine's limit is refused with a sentence, not a 422", async () => {
  const { client, narrate } = fakeClient();
  await renderShell(client);

  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "a".repeat(SOURCE_CHARACTER_LIMIT + 1) },
  });

  await waitFor(() =>
    expect(
      screen.getByText(/1 more than Readily can narrate at once/),
    ).toBeTruthy(),
  );
  expect(narrateButton().disabled).toBe(true);
  expect(narrate).not.toHaveBeenCalled();
});

test("a preparing Narration says so without inventing a wait", async () => {
  const { client } = fakeClient({ snapshots: [{ ...IDLE, phase: "preparing" }] });
  await renderShell(client);

  await waitFor(() =>
    expect(engineSaid("Getting the words ready…")).toBeTruthy(),
  );
});

test("a paused Narration is a sentence, not a dropped stream", async () => {
  // `paused` is a legal v1 phase (docs/wire.md).
  const { client } = fakeClient({ snapshots: [{ ...IDLE, phase: "paused" }] });
  await renderShell(client);

  await waitFor(() => expect(engineSaid("Paused.")).toBeTruthy());
});

test("an Engine that dies takes its Narration off the screen with it", async () => {
  const { client, connect } = fakeClient({
    snapshots: [{ ...IDLE, phase: "playing" }],
  });
  await renderShell(client);
  await waitFor(() => expect(engineSaid("Reading aloud…")).toBeTruthy());

  await connect({ state: "failed", message: "The Engine stopped responding." });

  expect(screen.getByText("The Engine stopped responding.")).toBeTruthy();
  expect(engineSaid("Reading aloud…")).toBe(null);
});

test("a restarting Engine stops claiming the last Narration is still playing", async () => {
  const { client, connect } = fakeClient({
    snapshots: [{ ...IDLE, phase: "playing" }],
  });
  await renderShell(client);
  await waitFor(() => expect(engineSaid("Reading aloud…")).toBeTruthy());

  await connect({ state: "starting", detail: "restarting" });

  expect(screen.getByText("Restarting the Engine…")).toBeTruthy();
  expect(engineSaid("Reading aloud…")).toBe(null);
});

test("only the newest request may say what went wrong", async () => {
  // One reject per call, so the *first* request is the one refused however
  // many have been asked for since.
  const refusals: ((error: Error) => void)[] = [];
  const narrate = vi.fn(
    () => new Promise<void>((_resolve, reject) => refusals.push(reject)),
  );
  const { client, emit } = fakeClient({ narrate });
  await renderShell(client);
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "A local Narration." },
  });
  await waitFor(() => expect(narrateButton().disabled).toBe(false));

  fireEvent.click(narrateButton());
  await waitFor(() => expect(narrate).toHaveBeenCalledOnce());
  fireEvent.click(narrateButton());
  await waitFor(() => expect(narrate).toHaveBeenCalledTimes(2));
  await emit({ ...IDLE, phase: "playing" });

  await act(async () => {
    refusals[0](new Error("The first Narration was refused."));
  });

  expect(screen.queryByText("The first Narration was refused.")).toBe(null);
  expect(engineSaid("Reading aloud…")).toBeTruthy();
});

test("with nothing downloaded, Narrate says so instead of offering a refusal", async () => {
  const { narrate } = await emptyDisk();
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "A local Narration." },
  });

  await waitFor(() =>
    expect(
      screen.getByText(
        "Readily needs the chosen voice model on disk to read with. Pick it from the model menu to download it.",
      ),
    ).toBeTruthy(),
  );
  expect(narrateButton().disabled).toBe(true);
  expect(narrate).not.toHaveBeenCalled();
});

test("a Voice Model downloaded is not the chosen one downloaded", async () => {
  const { client, narrate } = fakeClient({
    selection: { modelId: "kokoro:82m", voiceId: "af_heart" },
    models: [NOTHING_INSTALLED[0], BOTH_INSTALLED[1]],
  });
  await renderShell(client);
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "A local Narration." },
  });

  await waitFor(() =>
    expect(
      screen.getByText(
        "Readily needs the chosen voice model on disk to read with. Pick it from the model menu to download it.",
      ),
    ).toBeTruthy(),
  );
  expect(narrateButton().disabled).toBe(true);
  expect(narrate).not.toHaveBeenCalled();
});

test("the launch screen holds until the model store has answered", async () => {
  const { client, releaseModels } = fakeClient({ holdModels: true });
  render(<App client={client} />);

  expect(await screen.findByText("Nearly ready…")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Source" })).toBe(null);

  await releaseModels();
  expect(await screen.findByRole("textbox", { name: "Source" })).toBeTruthy();
});

test("a finished Narration keeps its line", async () => {
  const { client } = fakeClient({ snapshots: [{ ...IDLE, phase: "finished" }] });
  await renderShell(client);

  await waitFor(() =>
    expect(engineSaid("Finished reading.")).toBeTruthy(),
  );
});

test("a failed request says what the Engine said", async () => {
  const { client } = fakeClient({
    narrate: vi.fn(async () => {
      throw new Error("Narrating needs a Voice Model you have not downloaded.");
    }),
  });
  await renderShell(client);
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "A local Narration." },
  });
  await waitFor(() => expect(narrateButton().disabled).toBe(false));

  fireEvent.click(narrateButton());

  await waitFor(() =>
    expect(
      screen.getByText(
        "Narrating needs a Voice Model you have not downloaded.",
      ),
    ).toBeTruthy(),
  );
});

test("New Narration keeps an unsent draft", async () => {
  const { client } = fakeClient();
  await renderShell(client);
  const source = screen.getByRole("textbox", {
    name: "Source",
  }) as HTMLTextAreaElement;
  fireEvent.change(source, { target: { value: "Something to keep." } });

  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));

  expect(source.value).toBe("Something to keep.");
});

test("no meter, no Block table, no unactionable percentage", async () => {
  const { client } = fakeClient({
    snapshots: [{ ...IDLE, phase: "playing", positionSec: 3, totalSec: 12 }],
  });
  await renderShell(client);
  const container = document.body;
  await waitFor(() => expect(engineSaid("Reading aloud…")).toBeTruthy());

  expect(container.querySelector("progress")).toBe(null);
  expect(container.querySelector("meter")).toBe(null);
  expect(container.querySelector("table")).toBe(null);
  expect(container.textContent).not.toMatch(/%/);
});

test("History is one row per Narration, in the order the Engine gave them", async () => {
  const { client } = fakeClient({
    history: [
      { ...ENTRY, id: "n-2", sourcePreview: "The newest one." },
      { ...ENTRY, id: "n-1", sourcePreview: "The older one." },
    ],
  });
  await renderShell(client);

  await waitFor(() => expect(historyRows()).toHaveLength(2));
  expect(historyRows()[0].textContent).toMatch(/The newest one\./);
  expect(historyRows()[1].textContent).toMatch(/The older one\./);
});

test("a row admits the audio it no longer has and the text it skipped", async () => {
  const { client } = fakeClient({
    history: [{ ...ENTRY, audioPresent: false, hasGaps: true }],
  });
  await renderShell(client);

  await waitFor(() => expect(historyRows()).toHaveLength(1));
  const row = historyRows()[0];
  expect(within(row).getByText(/re-made when you play this/)).toBeTruthy();
  expect(within(row).getByText(/could not be read and was skipped/)).toBeTruthy();
});

test("selecting an evicted row opens it paused, and its marker clears once the Engine has", async () => {
  const { client, openNarration, resumeNarration, serve, emit } = fakeClient({
    history: [{ ...ENTRY, audioPresent: false }],
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  expect(within(historyRows()[0]).getByText(/re-made when you play this/)).toBeTruthy();

  fireEvent.click(openRow());

  await waitFor(() => expect(resumeNarration).toHaveBeenCalledWith("n-1", { paused: true }));
  expect(openNarration).toHaveBeenCalledWith("n-1");

  serve([{ ...ENTRY, audioPresent: true }]);
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1" });

  await waitFor(() =>
    expect(screen.queryByText(/re-made when you play this/)).toBe(null),
  );
});

test("an interrupted row that resumes mid-way keeps the marker it earned", async () => {
  const interrupted: HistoryEntry = {
    ...ENTRY,
    status: "interrupted",
    audioPresent: false,
    playheadSec: 30,
  };
  const { client, listHistory, resumeNarration, serve, emit } = fakeClient({
    history: [interrupted],
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));

  fireEvent.click(openRow());
  await waitFor(() => expect(resumeNarration).toHaveBeenCalledWith("n-1", { paused: true }));

  serve([interrupted]);
  const readsBefore = listHistory.mock.calls.length;
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1" });
  await waitFor(() =>
    expect(listHistory.mock.calls.length).toBeGreaterThan(readsBefore),
  );

  expect(
    within(historyRows()[0]).getByText(/re-made when you play this/),
  ).toBeTruthy();
});

test("a reopened Narration marks its skipped text where the words are missing", async () => {
  const flagged = { ...ENTRY, hasGaps: true };
  const { client } = fakeClient({
    history: [flagged],
    detail: detailOf(flagged, "One. Two. Three.", [
      {
        ordinal: 1,
        sourceStart: 5,
        sourceEnd: 9,
        errorCode: "synthesis_failed",
        createdAt: ENTRY.createdAt,
      },
    ]),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));

  fireEvent.click(openRow());

  const opened = await waitFor(() =>
    screen.getByRole("article", { name: "Opened Narration" }),
  );
  expect(opened.querySelector("mark")?.textContent).toContain("Two.");
  expect(within(opened).getByText("One.")).toBeTruthy();
  expect(within(opened).getByText("Three.")).toBeTruthy();
  expect(
    within(historyRows()[0]).getByText(/could not be read and was skipped/),
  ).toBeTruthy();
});

test("the banner counts the passages the text actually marks", async () => {
  const flagged = { ...ENTRY, hasGaps: true };
  const { client } = fakeClient({
    history: [flagged],
    detail: detailOf(flagged, "abcdef", [
      { ordinal: 1, sourceStart: 4, sourceEnd: 99, errorCode: "x", createdAt: ENTRY.createdAt },
      { ordinal: 2, sourceStart: 2, sourceEnd: 5, errorCode: "x", createdAt: ENTRY.createdAt },
      { ordinal: 3, sourceStart: -3, sourceEnd: 1, errorCode: "x", createdAt: ENTRY.createdAt },
    ]),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));

  fireEvent.click(openRow());

  const opened = await waitFor(() =>
    screen.getByRole("article", { name: "Opened Narration" }),
  );
  expect(opened.querySelectorAll("mark")).toHaveLength(2);
  expect(within(opened).getByText(/2 passages could not be read/)).toBeTruthy();
});

test("a Narration the Engine will not play again is still reopened, refusal and all", async () => {
  const { client } = fakeClient({
    history: [ENTRY],
    resumeNarration: vi.fn(async () => {
      throw new Error("That Narration's Voice Model is not downloaded.");
    }),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));

  fireEvent.click(openRow());

  await waitFor(() =>
    expect(
      screen.getByText("That Narration's Voice Model is not downloaded."),
    ).toBeTruthy(),
  );
  expect(screen.getByRole("article", { name: "Opened Narration" })).toBeTruthy();
});

test("deleting from the sidebar takes a second press, then says nothing", async () => {
  const { client, deleteNarration, serve } = fakeClient({
    history: [ENTRY],
    deleteNarration: vi.fn(async () => ({
      narrationId: "n-1",
      audioBytesFreed: 1024 * 1024,
    })),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));

  fireEvent.click(screen.getByRole("button", { name: "Delete The sea was calm." }));

  expect(deleteNarration).not.toHaveBeenCalled();
  serve([]);
  fireEvent.click(
    screen.getByRole("button", { name: "Delete The sea was calm. permanently" }),
  );

  await waitFor(() => expect(deleteNarration).toHaveBeenCalledWith("n-1"));
  await waitFor(() => expect(historyRows()).toHaveLength(0));
  // The gone row is the whole answer — a delete that worked posts no notice.
  expect(screen.queryByText(/freed/)).toBeNull();
});

test("History's rows are what make its scroll region reachable from the keyboard", async () => {
  // The container has no `tabindex`: a scrollable region whose children are
  // focusable is already keyboard-scrollable.
  const { client } = fakeClient({ history: [ENTRY] });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));

  const row = openRow();
  row.focus();

  expect(document.activeElement).toBe(row);
  expect(historyList().getAttribute("tabindex")).toBe(null);
});

const readAlong = () => screen.getByRole("article", { name: "Opened Narration" });
const blockOf = (words: string) =>
  within(readAlong()).getByText(words).closest("button");
const seekWord = (word: string) =>
  within(readAlong()).queryByRole("button", { name: word });

test("clicking a word seeks there, and the highlight follows the Engine", async () => {
  const { client, seek, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await waitFor(() => expect(readAlong()).toBeTruthy());

  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 0, totalSec: 13 });
  await waitFor(() =>
    expect(seekWord("One")?.closest(".opened__source > .opened__block")?.getAttribute("aria-current")).toBe("true"),
  );

  fireEvent.click(seekWord("Three")!);

  expect(seek).toHaveBeenCalledWith(10);
  expect(seekWord("One")?.closest(".opened__source > .opened__block")?.getAttribute("aria-current")).toBe("true");

  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 9.2, totalSec: 13 });

  await waitFor(() =>
    expect(seekWord("Three")?.closest(".opened__source > .opened__block")?.getAttribute("aria-current")).toBe("true"),
  );
  expect(seekWord("One")?.closest(".opened__source > .opened__block")?.getAttribute("aria-current")).toBe(null);
});

test("the next unmeasured Block is seekable while uncut Source stays readable", async () => {
  const { client, seek, emit } = fakeClient({
    history: [ENTRY],
    detail: detailOf(ENTRY, "One. Two. Three.", [], [
      segment(0, 0, 4, 0),
      segment(1, 5, 9, null),
    ]),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 4 });

  await waitFor(() => expect(seekWord("One")).toBeTruthy());
  fireEvent.click(seekWord("Two")!);
  expect(seek).toHaveBeenCalledWith(5);
  expect(seekWord("Three")).toBe(null);
});

test("the player's transport mirrors the Engine's phase rather than its own", async () => {
  const { client, pause, play, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 5, totalSec: 13 });

  const pauseButton = await waitFor(() => screen.getByRole("button", { name: "Pause" }));
  fireEvent.click(pauseButton);
  expect(pause).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Pause" })).toBeTruthy();

  await emit({ ...IDLE, phase: "paused", narrationId: "n-1", positionSec: 5, totalSec: 13 });

  const playButton = await waitFor(() => screen.getByRole("button", { name: "Play" }));
  fireEvent.click(playButton);
  expect(play).toHaveBeenCalledOnce();
});

test("playback speed changes live and follows the Engine snapshot", async () => {
  const { client, setSpeed, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1" });

  const speed = await screen.findByRole("combobox", { name: "Playback speed" });
  expect(speed).toBeInstanceOf(HTMLSelectElement);
  if (!(speed instanceof HTMLSelectElement)) {
    throw new Error("Expected a speed selector.");
  }
  fireEvent.change(speed, { target: { value: "2" } });
  expect(setSpeed).toHaveBeenCalledWith(2);
  expect(speed.value).toBe("1");

  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", speed: 2 });
  await waitFor(() => expect(speed.value).toBe("2"));
});

test("a stored Narration's player shows the Engine's speed, not the one it was made at", async () => {
  const { client, setSpeed, emit } = fakeClient({
    history: [{ ...ENTRY, speed: 1.5 }],
    detail: { ...THREE_BLOCKS, speed: 1.5 },
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, speed: 2 });

  const speed = await screen.findByRole("combobox", { name: "Playback speed" });
  await waitFor(() => expect((speed as HTMLSelectElement).value).toBe("2"));
  fireEvent.change(speed, { target: { value: "3" } });
  expect(setSpeed).toHaveBeenCalledWith(3);
});

test("History exports a Narration without opening or playing it", async () => {
  const { client, exportNarration, openNarration, resumeNarration } = fakeClient({
    history: [ENTRY],
  });
  await renderShell(client);
  fireEvent.click(await screen.findByRole("button", { name: "Export The sea was calm." }));
  await waitFor(() => expect(exportNarration).toHaveBeenCalledWith("n-1"));
  expect(openNarration).not.toHaveBeenCalled();
  expect(resumeNarration).not.toHaveBeenCalled();
  expect(await screen.findByText("Saving the audio to your Readily folder…")).toBeTruthy();
});

test("the player shows the clock, the Voice and the way out to a file", async () => {
  const { client, exportNarration, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
    models: BOTH_INSTALLED,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 5, totalSec: 13 });

  await waitFor(() => expect(screen.getByText("0:05")).toBeTruthy());
  expect(screen.getByText("1:02")).toBeTruthy();
  expect(document.querySelector(".player__clock")?.textContent).toBe("0:05 of 1:02");
  expect(document.querySelector(".player__voice")?.textContent).toBe("Heart·Kokoro");

  fireEvent.click(screen.getByRole("button", { name: "Export" }));

  await waitFor(() => expect(exportNarration).toHaveBeenCalledWith("n-1"));
  expect(screen.getByText("Saving the audio to your Readily folder…")).toBeTruthy();
});

test("the scrubber moves in seconds, and only as far as the Engine has assembled", async () => {
  // A Narration still being read, so its length is whatever the Engine has
  // assembled so far rather than a stored total.
  const reading = { ...ENTRY, status: "playing" as const, totalDurationSec: null };
  const { client, seek, seekTime, emit } = fakeClient({
    history: [reading],
    detail: detailOf(reading, "One. Two. Three.", [], [
      segment(0, 0, 4, 0),
      segment(1, 5, 9, 4),
    ]),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 2, totalSec: 8 });

  const scrubber = await waitFor(
    () => screen.getByRole("slider", { name: "Position" }) as HTMLInputElement,
  );
  // The track is the audio assembled so far, and the thumb is the playhead
  // on it: a second of track is a second of audio however the Blocks fall.
  expect(scrubber.max).toBe("8");
  expect(scrubber.value).toBe("2");
  expect(scrubber.getAttribute("aria-valuetext")).toBe("0:02 of 0:08");
  expect(scrubber.style.getPropertyValue("--player-fill")).toBe("25%");

  // A range input fires `change` on every value it passes through, so what
  // travels is the value the reader let go of, once — and the clock shows
  // where the thumb is while it is held.
  fireEvent.change(scrubber, { target: { value: "6" } });
  fireEvent.change(scrubber, { target: { value: "1" } });
  fireEvent.change(scrubber, { target: { value: "6" } });
  expect(seekTime).not.toHaveBeenCalled();
  expect(scrubber.getAttribute("aria-valuetext")).toBe("0:06 of 0:08");

  fireEvent.pointerUp(scrubber);

  expect(seekTime).toHaveBeenCalledOnce();
  expect(seekTime).toHaveBeenCalledWith(6);
  expect(seek).not.toHaveBeenCalled();

  // More audio arriving lengthens the track under the thumb rather than
  // moving the thumb.
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 6, totalSec: 16 });
  expect(scrubber.max).toBe("16");
  expect(scrubber.value).toBe("6");
});

test("a Narration started from the composer becomes the thing being read", async () => {
  const { client, emit } = fakeClient({ detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());

  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "One. Two. Three." },
  });
  fireEvent.click(narrateButton());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 4.5, totalSec: 13 });

  await waitFor(() =>
    expect(seekWord("Two")?.closest(".opened__source > .opened__block")?.getAttribute("aria-current")).toBe("true"),
  );
  expect(screen.queryByRole("textbox", { name: "Source" })).toBe(null);
});

test("cancelling a Narration being prepared stops it once and puts the composer back", async () => {
  let settle = () => {};
  const { client, stop, emit } = fakeClient({
    detail: THREE_BLOCKS,
    stop: vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    ),
  });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "One. Two. Three." },
  });
  fireEvent.click(narrateButton());
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1" });

  fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
  const cancelling = screen.getByRole("button", { name: "Cancelling…" });
  expect(cancelling.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(cancelling);
  expect(stop).toHaveBeenCalledOnce();

  settle();
  await emit(IDLE);
  await waitFor(() =>
    expect((screen.getByRole("textbox", { name: "Source" }) as HTMLTextAreaElement).value).toBe("One. Two. Three."),
  );
  expect(screen.queryByRole("button", { name: /^Cancel/ })).toBe(null);
});

test("a cancel the Engine refuses can be tried again", async () => {
  const { client, stop, emit } = fakeClient({
    detail: THREE_BLOCKS,
    stop: vi.fn(async () => {
      throw new Error("The Engine is busy.");
    }),
  });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "One. Two. Three." },
  });
  fireEvent.click(narrateButton());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });

  fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

  await waitFor(() => expect(engineSaid("The Engine is busy.")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(stop).toHaveBeenCalledTimes(2);
  expect(readAlong()).toBeTruthy();
});

test("a paused Narration offers no Cancel: nobody is waiting on it", async () => {
  const { client, emit } = fakeClient({ history: [ENTRY], detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "paused", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  expect(screen.queryByRole("button", { name: "Cancel" })).toBe(null);
});

test("the prepare line is shown once, under the player, and announced once", async () => {
  const { client, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1" });

  await waitFor(() =>
    expect(engineSaid("Getting the words ready…")).toBeTruthy(),
  );
  // The transport is disabled until the first words arrive, so this is the
  // one live sentence the row shows as well as announces — and nowhere else
  // repeats it.
  expect(screen.queryAllByText("Getting the words ready…")).toHaveLength(1);
  const announced = document.querySelector(".narration__line");
  expect(announced?.textContent).toBe("Getting the words ready…");
  expect(announced?.classList.contains("visually-hidden")).toBe(false);
});

test("a live player leaves the Narration's sentence to the transport, and only announces it", async () => {
  const { client, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "paused", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  const lead = document.querySelector(".player__lead");
  expect(lead?.querySelector(".player__voice")).toBeTruthy();
  expect(lead?.textContent).not.toContain("Paused.");
  const line = document.querySelector(".narration__line");
  expect(line?.textContent).toBe("Paused.");
  expect(line?.classList.contains("visually-hidden")).toBe(true);

  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 2, totalSec: 13 });
  await waitFor(() => expect(engineSaid("Reading aloud…")).toBeTruthy());
  expect(lead?.textContent).not.toContain("Reading aloud…");
});

test("closing a paused Narration keeps its live player beside the composer", async () => {
  const { client, stop, setSpeed, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "paused", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));

  expect(screen.getByRole("textbox", { name: "Source" })).toBeTruthy();
  expect(stop).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("combobox", { name: "Playback speed" }), {
    target: { value: "2" },
  });
  expect(setSpeed).toHaveBeenCalledWith(2);
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 6, totalSec: 13 });
  expect(engineSaid("Reading aloud…")).toBeTruthy();
  expect(screen.queryByRole("article", { name: "Opened Narration" })).toBe(null);
});

test("a Narration closed once can be opened again", async () => {
  const { client, emit } = fakeClient({ history: [ENTRY], detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));

  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "paused", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));
  expect(screen.getByRole("textbox", { name: "Source" })).toBeTruthy();

  fireEvent.click(openRow());

  await waitFor(() => expect(readAlong()).toBeTruthy());
});

test("opening a row never puts the Narration the reader just closed back", async () => {
  const SECOND: HistoryEntry = { ...ENTRY, id: "n-2", sourcePreview: "Another." };
  const { client, openNarration, emit } = fakeClient({
    history: [ENTRY, SECOND],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "paused", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  openNarration.mockClear();

  fireEvent.click(openRow(1));
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));

  await waitFor(() => expect(openNarration).toHaveBeenCalledWith("n-2"));
  expect(openNarration).not.toHaveBeenCalledWith("n-1");
});

test("a Narration that stops leaves its words on screen", async () => {
  const { client, emit } = fakeClient({ detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "One. Two. Three." },
  });
  fireEvent.click(narrateButton());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  await emit(IDLE);

  expect(readAlong()).toBeTruthy();
  expect(within(readAlong()).getByText("One.")).toBeTruthy();
});

test("an Export in flight never hides a Narration failing", async () => {
  const { client, emit } = fakeClient({ history: [ENTRY], detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });

  fireEvent.click(await screen.findByRole("button", { name: "Export" }));
  await waitFor(() => expect(screen.getByText("Saving the audio to your Readily folder…")).toBeTruthy());

  await emit({
    ...IDLE,
    phase: "failed",
    narrationId: "n-1",
    error: { version: 1, code: "synthesis_failed", message: "The Voice Model stopped responding." },
  });

  await waitFor(() =>
    expect(engineSaid("The Voice Model stopped responding.")).toBeTruthy(),
  );
  // Shown by the player, under its transport, and announced once from the
  // row below it — the Export's own notice is a different sentence about a
  // different thing, and both stay.
  expect(document.querySelector(".player__problem")?.textContent).toBe(
    "The Voice Model stopped responding.",
  );
  const line = document.querySelector(".narration--failed .narration__line");
  expect(line?.textContent).toBe("The Voice Model stopped responding.");
  expect(line?.classList.contains("visually-hidden")).toBe(true);
  expect(screen.getByText("Saving the audio to your Readily folder…")).toBeTruthy();
});

test("a reread that lands clears the one that did not", async () => {
  let call = 0;
  const openNarration = vi.fn(async () => {
    call += 1;
    if (call === 2) throw new Error("That Narration could not be read.");
    return THREE_BLOCKS;
  });
  const { client, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
    openNarration,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 4 });
  await waitFor(() =>
    expect(screen.getByText("That Narration could not be read.")).toBeTruthy(),
  );

  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 5, totalSec: 9 });

  await waitFor(() =>
    expect(screen.queryByText("That Narration could not be read.")).toBe(null),
  );
});

test("the player's notice is a region the reader's software is already watching", async () => {
  const { client, emit } = fakeClient({ history: [ENTRY], detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  const region = () => document.querySelector(".player__notice");
  const before = region();
  expect(before).toBeTruthy();
  expect(before?.textContent).toBe("");

  fireEvent.click(screen.getByRole("button", { name: "Export" }));

  await waitFor(() => expect(before?.textContent).toBe("Saving the audio to your Readily folder…"));
  expect(region()).toBe(before);
});

test("an Export's sentence stays with the Narration it was asked of", async () => {
  const SECOND: HistoryEntry = { ...ENTRY, id: "n-2", sourcePreview: "Another." };
  const { client, emit } = fakeClient({
    history: [ENTRY, SECOND],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Export" }));
  await waitFor(() => expect(screen.getByText("Saving the audio to your Readily folder…")).toBeTruthy());

  fireEvent.click(openRow(1));
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));

  await waitFor(() => expect(screen.queryByText("Saving the audio to your Readily folder…")).toBe(null));
});

test("a document nobody is reading highlights nothing and tells no story", async () => {
  const failed: HistoryEntry = { ...ENTRY, status: "failed" };
  const { client } = fakeClient({
    history: [failed],
    detail: { ...THREE_BLOCKS, status: "failed" },
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));

  fireEvent.click(openRow());

  await waitFor(() => expect(readAlong()).toBeTruthy());
  expect(within(readAlong()).getByText("One.")).toBeTruthy();
  expect(seekWord("One")).toBe(null);
  expect(engineSaid("Reading aloud…")).toBe(null);
  expect(
    (screen.getByRole("button", { name: "Play" }) as HTMLButtonElement).disabled,
  ).toBe(true);
});

test("a document the Engine is not reading cannot move the audio", async () => {
  const { client, seek, seekTime, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await waitFor(() => expect(readAlong()).toBeTruthy());

  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });

  await waitFor(() => expect(seekWord("Two")).toBe(null));
  fireEvent.click(within(readAlong()).getByText("Two."));

  const scrubber = screen.getByRole("slider", { name: "Position" }) as HTMLInputElement;
  expect(scrubber.disabled).toBe(true);
  fireEvent.change(scrubber, { target: { value: "1" } });
  fireEvent.pointerUp(scrubber);

  expect(seek).not.toHaveBeenCalled();
  expect(seekTime).not.toHaveBeenCalled();
});

test("a finished Narration is read again from wherever the reader moves it", async () => {
  // `narrationId` outlives `phase`, but the Engine refuses a seek in every
  // phase it refuses a Stop — silently, with `seeked: false`. What it will
  // do with a finished Narration is read it again, so that is what every
  // move of the playhead asks for: a silent replay, then — once the Engine
  // says the replay is what it is preparing — the seek.
  const { client, seek, seekTime, resumeNarration, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(seekWord("One")).toBeTruthy());
  resumeNarration.mockClear();

  const finished = { ...IDLE, phase: "finished", narrationId: "n-1", positionSec: 62, totalSec: 62 } as const;
  await emit(finished);

  const scrubber = screen.getByRole("slider", { name: "Position" }) as HTMLInputElement;
  await waitFor(() => expect(scrubber.disabled).toBe(false));
  expect(scrubber.value).toBe("62");
  expect(screen.getByRole("button", { name: "Forward 30 seconds" }).hasAttribute("disabled")).toBe(true);

  fireEvent.change(scrubber, { target: { value: "4" } });
  fireEvent.pointerUp(scrubber);
  await waitFor(() => expect(resumeNarration).toHaveBeenCalledWith("n-1", { paused: true }));
  expect(seekTime).not.toHaveBeenCalled();
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1", positionSec: 0, totalSec: 0 });
  expect(seekTime).toHaveBeenCalledOnce();
  expect(seekTime).toHaveBeenCalledWith(4);

  // Two clicks before the Engine has admitted the replay are one replay
  // with the later target: the Engine refuses to resume what it is
  // already preparing.
  await emit(finished);
  fireEvent.click(screen.getByRole("button", { name: "Back 15 seconds" }));
  fireEvent.click(screen.getByRole("button", { name: "Back 15 seconds" }));
  await waitFor(() => expect(resumeNarration).toHaveBeenCalledTimes(2));
  await emit({ ...IDLE, phase: "paused", narrationId: "n-1", positionSec: 0, totalSec: 62 });
  expect(seekTime).toHaveBeenCalledTimes(2);
  expect(seekTime).toHaveBeenLastCalledWith(47);

  await emit(finished);
  fireEvent.click(await waitFor(() => seekWord("Two") as HTMLElement));
  await waitFor(() => expect(resumeNarration).toHaveBeenCalledTimes(3));
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1", positionSec: 0, totalSec: 0 });
  expect(seek).toHaveBeenCalledWith(5);

  // Play alone is a replay from the top, out loud.
  await emit(finished);
  fireEvent.click(screen.getByRole("button", { name: "Play again" }));
  await waitFor(() => expect(resumeNarration).toHaveBeenLastCalledWith("n-1", { paused: false }));
  // A second Play before the Engine has admitted it is not a second replay
  // — the Engine would refuse one — and a move after it rides the same one.
  fireEvent.click(screen.getByRole("button", { name: "Play again" }));
  fireEvent.click(screen.getByRole("button", { name: "Back 15 seconds" }));
  expect(resumeNarration).toHaveBeenCalledTimes(4);
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1", positionSec: 0, totalSec: 0 });
  expect(seekTime).toHaveBeenCalledTimes(3);
  expect(seekTime).toHaveBeenLastCalledWith(47);

  // Play after a move has asked for a silent replay: the Engine only knows
  // Play as the release of a pause, so it becomes a seek to the top, which
  // the Engine starts reading from.
  await emit(finished);
  fireEvent.click(screen.getByRole("button", { name: "Back 15 seconds" }));
  fireEvent.click(screen.getByRole("button", { name: "Play again" }));
  await waitFor(() => expect(resumeNarration).toHaveBeenCalledTimes(5));
  expect(resumeNarration).toHaveBeenLastCalledWith("n-1", { paused: true });
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1", positionSec: 0, totalSec: 0 });
  expect(seekTime).toHaveBeenCalledTimes(4);
  expect(seekTime).toHaveBeenLastCalledWith(0);
});

test("a move waiting on a replay is dropped when another Narration is admitted instead", async () => {
  // Between asking for the replay and the Engine preparing it, a reader can
  // open another row. A seek moves whatever is active, so the waiting move
  // must not land on the newcomer — or on the finished Narration later.
  const { client, seekTime, resumeNarration, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "finished", narrationId: "n-1", positionSec: 62, totalSec: 62 });

  fireEvent.click(await screen.findByRole("button", { name: "Back 15 seconds" }));
  await waitFor(() => expect(resumeNarration).toHaveBeenCalledWith("n-1", { paused: true }));

  await emit({ ...IDLE, phase: "preparing", narrationId: "n-2", positionSec: 0, totalSec: 0 });
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1", positionSec: 0, totalSec: 0 });
  expect(seekTime).not.toHaveBeenCalled();

  // A replay that fails to prepare takes its move with it too: the reader
  // who later opens the row again, silent, must not find it lurching to
  // where they once dragged.
  await emit({ ...IDLE, phase: "finished", narrationId: "n-1", positionSec: 62, totalSec: 62 });
  resumeNarration.mockClear();
  fireEvent.click(await screen.findByRole("button", { name: "Back 15 seconds" }));
  await waitFor(() => expect(resumeNarration).toHaveBeenCalledOnce());
  await emit({ ...IDLE, phase: "failed", narrationId: "n-1", positionSec: 0, totalSec: 0, error: { version: 1, code: "synthesis_failed", message: "It broke." } });
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1", positionSec: 0, totalSec: 0 });
  expect(seekTime).not.toHaveBeenCalled();
});

test("a replay the Engine refuses does not seek, and does not jam the next one", async () => {
  const { client, seekTime, resumeNarration, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "finished", narrationId: "n-1", positionSec: 62, totalSec: 62 });
  resumeNarration.mockClear();
  resumeNarration.mockRejectedValueOnce(new Error("That Narration cannot be resumed."));

  fireEvent.click(await screen.findByRole("button", { name: "Back 15 seconds" }));

  // A refusal is a problem the player shows itself, under its transport.
  await waitFor(() =>
    expect(document.querySelector(".player__problem")?.textContent).toBe(
      "That Narration cannot be resumed.",
    ),
  );
  expect(seekTime).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Back 15 seconds" }));
  await waitFor(() => expect(resumeNarration).toHaveBeenCalledTimes(2));
});

test("selecting a sentence to copy it does not move the audio", async () => {
  // A selection drag that begins and ends inside one Block arrives as a click.
  const { client, seek, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });

  const block = await waitFor(() => seekWord("Two") as HTMLElement);
  const held = (text: string) => {
    vi.spyOn(window, "getSelection").mockReturnValue({
      toString: () => text,
    } as unknown as Selection);
  };

  held("Two.");
  fireEvent.click(block);
  expect(seek).not.toHaveBeenCalled();

  held("T");
  fireEvent.click(block);
  expect(seek).toHaveBeenCalledWith(5);

  vi.mocked(window.getSelection).mockRestore();
});

test("New Narration while a Narration is generating asks before cancelling it", async () => {
  let settle = () => {};
  const { client, stop, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
    stop: vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    ),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));
  expect(screen.getByText("Still generating")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Keep generating" }));
  expect(screen.queryByText("Still generating")).toBe(null);
  expect(stop).not.toHaveBeenCalled();
  expect(readAlong()).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));
  expect(stop).toHaveBeenCalledOnce();
  expect(readAlong()).toBeTruthy();

  settle();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Source" })).toBeTruthy());
  await waitFor(() => expect(screen.queryByText("Still generating")).toBe(null));
});

test("New Narration while playing audio the Engine has finished making asks nothing", async () => {
  const { client, stop, emit } = fakeClient({ history: [ENTRY], detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({
    ...IDLE,
    phase: "playing",
    narrationId: "n-1",
    positionSec: 1,
    totalSec: 13,
    diagnostics: { ...QUIET, generationComplete: true },
  });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));
  expect(screen.queryByText("Still generating")).toBe(null);
  expect(stop).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Source" })).toBeTruthy());
});

test("the question answers itself when generation finishes while playback continues", async () => {
  const { client, stop, emit } = fakeClient({ history: [ENTRY], detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));
  expect(screen.getByText("Still generating")).toBeTruthy();

  await emit({
    ...IDLE,
    phase: "playing",
    narrationId: "n-1",
    positionSec: 2,
    totalSec: 13,
    diagnostics: { ...QUIET, generationComplete: true },
  });
  await waitFor(() => expect(screen.queryByText("Still generating")).toBe(null));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Source" })).toBeTruthy());
  expect(stop).not.toHaveBeenCalled();
});

test("narrating something else while a Narration is generating narrates only once it is cancelled", async () => {
  let settle = () => {};
  const { client, stop, narrate, emit } = fakeClient({
    history: [ENTRY],
    detail: THREE_BLOCKS,
    stop: vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    ),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "paused", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  await waitFor(() => expect(readAlong()).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 1, totalSec: 13 });
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "Something else." },
  });

  fireEvent.click(narrateButton());
  expect(screen.getByText("Still generating")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));
  expect(stop).toHaveBeenCalledOnce();
  expect(narrate).not.toHaveBeenCalled();

  settle();
  await waitFor(() =>
    expect(narrate).toHaveBeenCalledWith("Something else.", expect.anything(), expect.anything()),
  );
});

test("opening a document beside a paused one replaces it without asking", async () => {
  // Rows open paused, so a reader moving from row to row has not heard
  // anything Stop could cut off; the question would be about nothing.
  const { client, stop, openNarration, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: "Second one." }],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  await emit({ ...IDLE, phase: "paused", narrationId: "n-2", positionSec: 0, totalSec: 9 });
  expect(screen.getByRole("button", { name: "Reading mode: Advanced" }).hasAttribute("disabled")).toBe(false);

  fireEvent.click(openRow());
  expect(screen.queryByText("Still generating")).toBe(null);
  await waitFor(() => expect(openNarration).toHaveBeenCalledWith("n-1"));
  expect(stop).not.toHaveBeenCalled();
});

test("opening a document while another is read asks before stopping it", async () => {
  const { client, stop, openNarration, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: "Second one." }],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });

  fireEvent.click(openRow());
  expect(screen.getByText("Still generating")).toBeTruthy();
  expect(screen.getByText("Generating “Second one.” is still in progress. Continuing will cancel it.")).toBeTruthy();
  expect(openNarration).not.toHaveBeenCalledWith("n-1");

  fireEvent.click(screen.getByRole("button", { name: "Keep generating" }));
  expect(screen.queryByText("Still generating")).toBe(null);
  expect(stop).not.toHaveBeenCalled();

  fireEvent.click(openRow());
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));
  await waitFor(() => expect(stop).toHaveBeenCalledOnce());
  await waitFor(() => expect(openNarration).toHaveBeenCalledWith("n-1"));
});

test("the other row opens only once the Engine has actually stopped", async () => {
  let settle = () => {};
  const { client, openNarration, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: "Second one." }],
    detail: THREE_BLOCKS,
    stop: vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    ),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });

  fireEvent.click(openRow());
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));
  expect(screen.getByText("Still generating")).toBeTruthy();
  expect(openNarration).not.toHaveBeenCalledWith("n-1");

  settle();
  await waitFor(() => expect(openNarration).toHaveBeenCalledWith("n-1"));
  await waitFor(() => expect(screen.queryByText("Still generating")).toBe(null));
});

test("a stop the Engine refuses leaves the reader where they were", async () => {
  const { client, openNarration, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: "Second one." }],
    detail: THREE_BLOCKS,
    stop: vi.fn(async () => {
      throw new Error("The Engine is busy.");
    }),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });

  fireEvent.click(openRow());
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));

  await waitFor(() => expect(engineSaid("The Engine is busy.")).toBeTruthy());
  expect(openNarration).not.toHaveBeenCalledWith("n-1");
});

test("the row opens on its own when the Narration the prompt asks about ends", async () => {
  const { client, stop, openNarration, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: "Second one." }],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });

  fireEvent.click(openRow());
  expect(screen.getByText("Still generating")).toBeTruthy();
  expect(openNarration).not.toHaveBeenCalledWith("n-1");

  await emit({ ...IDLE, phase: "finished", narrationId: "n-2" });
  await waitFor(() => expect(screen.queryByText("Still generating")).toBe(null));
  await waitFor(() => expect(openNarration).toHaveBeenCalledWith("n-1"));
  expect(stop).not.toHaveBeenCalled();
});

test("the prompt outlives an Engine that drops, and the row opens once it is back", async () => {
  const { client, connect, openNarration, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: "Second one." }],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });
  fireEvent.click(openRow());
  expect(screen.getByText("Still generating")).toBeTruthy();

  await connect({ state: "starting", detail: "restarting" });
  expect(screen.getByText("Still generating")).toBeTruthy();
  expect(openNarration).not.toHaveBeenCalledWith("n-1");

  await connect({ state: "ready" } as Connection);
  expect(screen.getByText("Still generating")).toBeTruthy();
  expect(openNarration).not.toHaveBeenCalledWith("n-1");

  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 4, totalSec: 9 });
  expect(screen.getByText("Still generating")).toBeTruthy();
  expect(openNarration).not.toHaveBeenCalledWith("n-1");

  await emit({ ...IDLE, phase: "finished", narrationId: "n-2" });
  await waitFor(() => expect(openNarration).toHaveBeenCalledWith("n-1"));
  await waitFor(() => expect(screen.queryByText("Still generating")).toBe(null));
});

test("the prompt names the Narration the way its row does", async () => {
  const { client, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: "Second\n\n  one.  " }],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });

  fireEvent.click(openRow());
  expect(screen.getByText("Generating “Second one.” is still in progress. Continuing will cancel it.")).toBeTruthy();
});

test("the line a reader is told the Narration by is never remounted", async () => {
  // An `aria-live` region that arrives already holding its first message is
  // announced unreliably, so it must be mounted before there is anything to say.
  const { client, emit } = fakeClient({ detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());

  const region = () => document.querySelector(".narration__line");
  const before = region();
  expect(before).toBeTruthy();
  expect(before?.textContent).toBe("");

  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "One. Two. Three." },
  });
  fireEvent.click(narrateButton());
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1" });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  expect(region()).toBe(before);
  expect(before?.textContent).toBe("Getting the words ready…");
});

test("a word's control is named by that word, and the gap beside it is still announced", async () => {
  const long = "A".repeat(120);
  const source = `${long} BBB`;
  const { client, emit } = fakeClient({
    history: [ENTRY],
    detail: detailOf(
      ENTRY,
      source,
      [
        {
          ordinal: 0,
          sourceStart: long.length + 1,
          sourceEnd: source.length,
          errorCode: "synthesis_failed",
          createdAt: new Date().toISOString(),
        },
      ],
      [segment(0, 0, source.length, 0)],
    ),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec: 0, totalSec: 8 });

  const word = await waitFor(() => blockOf(long));
  expect(word).toBeTruthy();
  expect(word?.getAttribute("aria-label")).toBe(null);
  expect(word?.textContent).toBe(long);
  const block = word?.closest(".opened__source > .opened__block");
  expect(block?.textContent).toContain("Start of skipped text:");
  expect(block?.textContent).toContain("BBB");
  expect(seekWord("BBB")).toBe(null);
});

test("closing a reopened Narration gives the composer back", async () => {
  const { client } = fakeClient({ history: [ENTRY] });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await waitFor(() =>
    expect(screen.getByRole("article", { name: "Opened Narration" })).toBeTruthy(),
  );
  expect(screen.queryByRole("textbox", { name: "Source" })).toBe(null);

  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));

  expect(screen.getByRole("textbox", { name: "Source" })).toBeTruthy();
});

test("New Narration leaves a reopened Narration for the draft it interrupted", async () => {
  const { client } = fakeClient({ history: [ENTRY] });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "Half written." },
  });
  fireEvent.click(openRow());
  await waitFor(() =>
    expect(screen.getByRole("article", { name: "Opened Narration" })).toBeTruthy(),
  );

  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));

  const source = screen.getByRole("textbox", {
    name: "Source",
  }) as HTMLTextAreaElement;
  expect(source.value).toBe("Half written.");
});

test("the row the Engine is playing is the row that says so", async () => {
  const { client, emit } = fakeClient({ history: [ENTRY, { ...ENTRY, id: "n-2" }] });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));

  await emit({ ...IDLE, phase: "playing", narrationId: "n-2" });

  expect(openRow(0).getAttribute("aria-current")).toBe(null);
  expect(openRow(1).getAttribute("aria-current")).toBe("true");
});

const popover = () => screen.getByRole("group", { name: "Voice Models" });

const voiceShown = async (model: string, voice: string) => {
  await screen.findByRole("button", { name: `Voice Model: ${model}` });
  await waitFor(() =>
    expect(document.querySelector(".carousel__voice")?.textContent).toBe(voice),
  );
};

const modelRowIn = (name: string) =>
  within(popover()).getByRole("button", { name: new RegExp(`^${name}`) });

const ONLY_QWEN_INSTALLED: ModelStatus[] = [
  {
    id: "kokoro:82m",
    installed: false,
    diskBytes: 0,
    downloadBytes: KOKORO_BYTES,
  },
  {
    id: "qwen3-tts:0.6b",
    installed: true,
    diskBytes: QWEN_BYTES,
    downloadBytes: QWEN_BYTES,
  },
];

const onQwen = (overrides: Partial<NarrationState> = {}): NarrationState => ({
  ...IDLE,
  phase: "preparing",
  narrationId: "n-1",
  modelId: "qwen3-tts:0.6b",
  voiceId: "Chelsie",
  ...overrides,
});

const fasterOffer = () =>
  screen.queryByRole("button", { name: "Getting impatient? Try Kokoro instead" });

test("an expressive wait offers the fast Voice, and takes the offer back once it is ahead", async () => {
  const { client, emit } = fakeClient({
    models: BOTH_INSTALLED,
    selection: { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" },
  });
  await renderShell(client);
  await voiceShown("Qwen3 TTS", "Chelsie");

  await emit(onQwen());

  await waitFor(() => expect(fasterOffer()).toBeTruthy());

  await emit(onQwen({ phase: "playing", positionSec: 8, totalSec: 12 }));
  expect(fasterOffer()).toBeTruthy();

  await emit(onQwen({ phase: "playing", positionSec: 8, totalSec: 90 }));
  await waitFor(() => expect(fasterOffer()).toBe(null));
});

test("the offer sits beside the Voice and takes nothing else's place", async () => {
  const { client, emit } = fakeClient({
    models: BOTH_INSTALLED,
    selection: { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" },
  });
  await renderShell(client);
  await voiceShown("Qwen3 TTS", "Chelsie");

  await emit(onQwen());
  await waitFor(() => expect(fasterOffer()).toBeTruthy());

  const lead = document.querySelector(".player__lead");
  expect(lead?.contains(fasterOffer())).toBe(true);
  expect(lead?.querySelector(".player__voice")).toBeTruthy();
  // The wait itself is still told from the row under the player.
  const line = document.querySelector(".narration__line");
  expect(line?.textContent).toBe("Getting the words ready…");
  expect(line?.classList.contains("visually-hidden")).toBe(false);
});

test("a Narration the Engine has finished assembling is never a wait", async () => {
  const { client, emit } = fakeClient({
    models: BOTH_INSTALLED,
    detail: THREE_BLOCKS,
    selection: { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" },
  });
  await renderShell(client);
  await voiceShown("Qwen3 TTS", "Chelsie");

  await emit(onQwen({ phase: "playing", positionSec: 11, totalSec: 13 }));

  await waitFor(() => expect(engineSaid("Reading aloud…")).toBeTruthy());
  expect(fasterOffer()).toBe(null);
});

test("the fast Tier is never offered an escape from itself", async () => {
  const { client, emit } = fakeClient({ models: BOTH_INSTALLED });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");

  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1" });

  await waitFor(() =>
    expect(engineSaid("Getting the words ready…")).toBeTruthy(),
  );
  expect(fasterOffer()).toBe(null);
});

test("with only one Voice Model on disk there is no escape hatch to offer", async () => {
  const { client, emit } = fakeClient({
    models: ONLY_QWEN_INSTALLED,
    selection: { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" },
  });
  await renderShell(client);
  await voiceShown("Qwen3 TTS", "Chelsie");

  await emit(onQwen());

  await waitFor(() =>
    expect(engineSaid("Getting the words ready…")).toBeTruthy(),
  );
  expect(fasterOffer()).toBe(null);
});

test("taking the offer rereads the same Source on the fast Voice, keeping the old one", async () => {
  const { client, emit, narrate, selectVoice, deleteNarration } = fakeClient({
    models: BOTH_INSTALLED,
    selection: { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" },
  });
  await renderShell(client);
  await voiceShown("Qwen3 TTS", "Chelsie");
  await emit(onQwen());
  await waitFor(() => expect(fasterOffer()).toBeTruthy());

  fireEvent.click(fasterOffer() as HTMLElement);

  await waitFor(() =>
    expect(narrate).toHaveBeenCalledWith("The sea was calm.", {
      modelId: "kokoro:82m",
      voiceId: "af_heart",
    }, "advanced"),
  );
  await waitFor(() =>
    expect(selectVoice).toHaveBeenCalledWith({
      modelId: "kokoro:82m",
      voiceId: "af_heart",
    }),
  );
  expect(deleteNarration).not.toHaveBeenCalled();
});

test("the pill names the Voice Model the Engine remembers, and lists the rest", async () => {
  const { client } = fakeClient({
    models: BOTH_INSTALLED,
    selection: { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" },
  });
  await renderShell(client);

  await voiceShown("Qwen3 TTS", "Chelsie");
  await openPopover();

  expect(modelRowIn("Qwen3 TTS").getAttribute("aria-current")).toBe("true");
  expect(modelRowIn("Kokoro").getAttribute("aria-current")).toBe(null);
  expect(within(popover()).getByText("instant")).toBeTruthy();
  expect(within(popover()).getByText("expressive")).toBeTruthy();
  expect(within(popover()).queryByRole("button", { name: /^Download / })).toBe(null);
});

test("switching Voice Model tells the Engine, so the choice outlives the session", async () => {
  const { client, selectVoice } = fakeClient({ models: BOTH_INSTALLED });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");
  await openPopover();

  fireEvent.click(modelRowIn("Qwen3 TTS"));

  await waitFor(() =>
    expect(selectVoice).toHaveBeenCalledWith({
      modelId: "qwen3-tts:0.6b",
      voiceId: "Chelsie",
    }),
  );
  await voiceShown("Qwen3 TTS", "Chelsie");
  expect(screen.queryByRole("group", { name: "Voice Models" })).toBe(null);
});

test("coming back to a Voice Model lands on the Voice it was last left on", async () => {
  const [kokoro, ...rest] = CATALOG.models;
  const bella = { simple: true, id: "af_bella", name: "Bella", language: "en-US", preview: null };
  const catalog = {
    ...CATALOG,
    models: [{ ...kokoro, voices: [...kokoro.voices, bella] }, ...rest],
  };
  const { client } = fakeClient({ catalog, models: BOTH_INSTALLED });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");

  fireEvent.click(screen.getByRole("button", { name: "Next voice" }));
  await voiceShown("Kokoro", "Bella");
  await openPopover();
  fireEvent.click(modelRowIn("Qwen3 TTS"));
  await voiceShown("Qwen3 TTS", "Chelsie");
  await openPopover();
  fireEvent.click(modelRowIn("Kokoro"));

  await voiceShown("Kokoro", "Bella");
});

test("a Voice the Engine will not store leaves the pill honest and says why", async () => {
  const { client } = fakeClient({
    models: BOTH_INSTALLED,
    selectVoice: vi.fn(async () => {
      throw new Error("That Voice Model is no longer in the Catalog.");
    }),
  });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");
  await openPopover();

  fireEvent.click(modelRowIn("Qwen3 TTS"));

  await waitFor(() =>
    expect(
      screen.getByText("That Voice Model is no longer in the Catalog."),
    ).toBeTruthy(),
  );
  await voiceShown("Kokoro", "Heart");
});

test("a chosen Voice that could not be read is not a licence to narrate with any", async () => {
  const { client } = fakeClient({
    models: ONLY_QWEN_INSTALLED,
    voiceFails: true,
  });
  await renderShell(client);
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "A local Narration." },
  });

  await waitFor(() =>
    expect(screen.getByText("The voice settings table is missing.")).toBeTruthy(),
  );
  expect(narrateButton().disabled).toBe(true);

  await openPopover();
  fireEvent.click(modelRowIn("Qwen3 TTS"));

  await voiceShown("Qwen3 TTS", "Chelsie");
  await waitFor(() => expect(narrateButton().disabled).toBe(false));
  expect(screen.queryByText("The voice settings table is missing.")).toBe(null);
});

test("the chosen Voice auditions under its orb, without a Narration", async () => {
  const play = vi
    .spyOn(window.HTMLMediaElement.prototype, "play")
    .mockResolvedValue(undefined);
  const { client, narrate } = fakeClient({ models: BOTH_INSTALLED });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");

  fireEvent.click(screen.getByRole("button", { name: "Hear Heart" }));

  await waitFor(() => expect(play).toHaveBeenCalled());
  const element = play.mock.instances[0] as HTMLAudioElement;
  expect(new URL(element.src).pathname).toBe("/previews/kokoro/82m/af_heart.m4a");
  expect(narrate).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Stop Heart" })).toBeTruthy();
  play.mockRestore();
});

test("with nothing downloaded the menu marks every model, and offers the Catalog", async () => {
  await emptyDisk();

  await openPopover();

  expect(within(popover()).getAllByRole("button", { name: /^Download / })).toHaveLength(2);
  expect(
    within(popover()).getByRole("button", { name: "Manage voice models…" }),
  ).toBeTruthy();
});

test("choosing a model that is not on disk opens the Catalog instead", async () => {
  await emptyDisk();
  await openPopover();

  fireEvent.click(modelRowIn("Qwen3 TTS"));

  await screen.findByRole("dialog", { name: "Voice models" });
  expect(screen.queryByRole("group", { name: "Voice Models" })).toBe(null);
});

test("the menu's download button starts that model's download in place", async () => {
  const { downloadModel, emitDownload } = await emptyDisk();
  await openPopover();

  const download = within(popover()).getByRole("button", { name: "Download Kokoro" });
  download.focus();
  fireEvent.click(download);

  await waitFor(() => expect(downloadModel).toHaveBeenCalledWith("kokoro:82m"));
  await emitDownload(downloadingKokoro());
  const running = await within(popover()).findByRole("button", { name: "Downloading Kokoro" });
  // The same button, still focused, so a keyboard reader keeps their place.
  expect(running).toBe(download);
  expect(document.activeElement).toBe(download);
  // Asking for a second download queues it behind the first.
  expect(
    within(popover()).getByRole("button", { name: "Download Qwen3 TTS" }),
  ).toHaveProperty("disabled", false);
});

test("a model waiting in the queue can be taken back out from the menu", async () => {
  const { emitDownload, withdrawModel } = await emptyDisk();
  await openPopover();

  await emitDownload(
    downloadingKokoro({ queue: [{ modelId: "qwen3-tts:0.6b", action: "download" }] }),
  );
  fireEvent.click(
    within(popover()).getByRole("button", { name: "Remove Qwen3 TTS from the queue" }),
  );

  await waitFor(() => expect(withdrawModel).toHaveBeenCalledWith("qwen3-tts:0.6b"));
});

test("a download started from the menu says why it failed, in the menu", async () => {
  const { emitDownload } = await emptyDisk();
  await openPopover();

  fireEvent.click(within(popover()).getByRole("button", { name: "Download Kokoro" }));
  await emitDownload(failedKokoro());

  await waitFor(() =>
    expect(within(popover()).getByRole("status").textContent).toBe(
      "Kokoro: The download could not be completed. Trying again picks up where it stopped.",
    ),
  );
});

test("the menu's trash button arms first, then deletes that model", async () => {
  const { client, deleteModel } = fakeClient({ models: BOTH_INSTALLED });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");
  await openPopover();

  fireEvent.click(within(popover()).getByRole("button", { name: "Delete Qwen3 TTS" }));
  expect(deleteModel).not.toHaveBeenCalled();
  fireEvent.click(
    within(popover()).getByRole("button", { name: "Confirm delete Qwen3 TTS" }),
  );

  await waitFor(() => expect(deleteModel).toHaveBeenCalledWith("qwen3-tts:0.6b"));
});

test("an armed trash button is disarmed when the popover closes", async () => {
  const { client } = fakeClient({ models: BOTH_INSTALLED });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");
  await openPopover();

  fireEvent.click(within(popover()).getByRole("button", { name: "Delete Qwen3 TTS" }));
  // A click on nothing focusable outside the popover, which fires no blur.
  fireEvent.pointerDown(document.body);
  await waitFor(() => expect(screen.queryByRole("group", { name: "Voice Models" })).toBe(null));
  await openPopover();

  expect(within(popover()).getByRole("button", { name: "Delete Qwen3 TTS" })).toBeTruthy();
});

test("Escape closes the popover and gives the pill its focus back", async () => {
  const { client } = fakeClient({ models: BOTH_INSTALLED });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");
  const pill = await openPopover().then(() => voicePill());

  fireEvent.keyDown(popover(), { key: "Escape" });

  await waitFor(() =>
    expect(screen.queryByRole("group", { name: "Voice Models" })).toBe(null),
  );
  expect(document.activeElement).toBe(pill);
});

test("a download that finishes after the sheet is closed still reaches the pill", async () => {
  const { serveModels, emitDownload } = await emptyDisk();
  await openSheet();
  fireEvent.click(
    within(modelRow("Kokoro")).getByRole("button", { name: /^Download/ }),
  );
  emitDownload(downloadingKokoro());
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Voice models" })).toBe(null),
  );

  serveModels([BOTH_INSTALLED[0], NOTHING_INSTALLED[1]]);
  emitDownload(
    downloadingKokoro({ phase: "installed", bytesDownloaded: KOKORO_BYTES }),
  );

  await openPopover();
  await waitFor(() =>
    expect(within(popover()).queryByRole("button", { name: "Download Kokoro" })).toBe(null),
  );
  expect(within(popover()).getByRole("button", { name: "Download Qwen3 TTS" })).toBeTruthy();
});

test("stepping to another Voice stops the one auditioning", async () => {
  const play = vi
    .spyOn(window.HTMLMediaElement.prototype, "play")
    .mockResolvedValue(undefined);
  const pause = vi.spyOn(window.HTMLMediaElement.prototype, "pause");
  const [kokoro, ...rest] = CATALOG.models;
  const bella = { simple: true, id: "af_bella", name: "Bella", language: "en-US", preview: null };
  const catalog = { ...CATALOG, models: [{ ...kokoro, voices: [...kokoro.voices, bella] }, ...rest] };
  const { client } = fakeClient({ catalog });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");
  fireEvent.click(screen.getByRole("button", { name: "Hear Heart" }));
  await waitFor(() => expect(play).toHaveBeenCalled());

  fireEvent.click(screen.getByRole("button", { name: "Next voice" }));

  await waitFor(() => expect(pause).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: /^Stop / })).toBe(null);
  play.mockRestore();
  pause.mockRestore();
});

test("opening a Narration stops the Voice it was auditioning", async () => {
  const play = vi
    .spyOn(window.HTMLMediaElement.prototype, "play")
    .mockResolvedValue(undefined);
  const pause = vi.spyOn(window.HTMLMediaElement.prototype, "pause");
  const { client } = fakeClient({ models: BOTH_INSTALLED, history: [ENTRY] });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");
  fireEvent.click(screen.getByRole("button", { name: "Hear Heart" }));
  await waitFor(() => expect(play).toHaveBeenCalled());

  fireEvent.click(openRow(0));

  await waitFor(() => expect(pause).toHaveBeenCalled());
  play.mockRestore();
  pause.mockRestore();
});

test.each([
  ["the Catalog", () => openSheet()],
  ["Settings", () => openSettings()],
  [
    "the stop prompt",
    async () => {
      fireEvent.click(openRow());
      await screen.findByText("Still generating");
    },
  ],
])("opening %s stops the Voice the carousel was auditioning", async (_, open) => {
  const play = vi
    .spyOn(window.HTMLMediaElement.prototype, "play")
    .mockResolvedValue(undefined);
  const pause = vi.spyOn(window.HTMLMediaElement.prototype, "pause");
  const { client, emit } = fakeClient({ models: BOTH_INSTALLED, history: [ENTRY] });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  await emit({ ...IDLE, phase: "paused", narrationId: "n-2", positionSec: 3, totalSec: 9 });
  fireEvent.click(screen.getByRole("button", { name: "New Narration" }));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });
  fireEvent.click(await screen.findByRole("button", { name: "Hear Heart" }));
  await waitFor(() => expect(play).toHaveBeenCalled());

  await open();

  await waitFor(() => expect(pause).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: "Stop Heart" })).toBe(null);
  play.mockRestore();
  pause.mockRestore();
});

test("a Voice with no clip yet has nothing to press under its orb", async () => {
  const { client } = fakeClient({
    models: BOTH_INSTALLED,
    selection: { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" },
  });
  await renderShell(client);
  await voiceShown("Qwen3 TTS", "Chelsie");

  expect(screen.queryByRole("button", { name: "Hear Chelsie" })).toBe(null);
});

test("a Voice Model deleted in the sheet is marked as gone in the menu", async () => {
  const { client, serveModels } = fakeClient({ models: BOTH_INSTALLED });
  await renderShell(client);
  await voiceShown("Kokoro", "Heart");
  await openSheet();

  const arm = await waitFor(() =>
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: /^Delete/ }),
  );
  fireEvent.click(arm);
  serveModels([BOTH_INSTALLED[0], NOTHING_INSTALLED[1]]);
  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Delete?" }),
  );
  await waitFor(() => expect(screen.getByText(/Qwen3 TTS deleted/)).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Done" }));

  await openPopover();

  expect(within(popover()).getByRole("button", { name: "Download Qwen3 TTS" })).toBeTruthy();
  expect(within(popover()).queryByRole("button", { name: "Download Kokoro" })).toBe(null);
});

const voicePill = () => screen.getByRole("button", { name: /^Voice Model/ });

const openPopover = async () => {
  fireEvent.click(voicePill());
  return await screen.findByRole("group", { name: "Voice Models" });
};

const openSheet = async () => {
  await openPopover();
  fireEvent.click(
    screen.getByRole("button", { name: "Manage voice models…" }),
  );
  await screen.findByRole("dialog", { name: "Voice models" });
};

const modelRow = (name: string) =>
  within(screen.getByRole("dialog", { name: "Voice models" }))
    .getAllByRole("listitem")
    .find((row) => within(row).queryByText(name)) as HTMLElement;

test("the Catalog sheet lists every entry, downloaded or not", async () => {
  await emptyDisk();

  await openSheet();

  const kokoro = modelRow("Kokoro");
  expect(within(kokoro).getByText("instant")).toBeTruthy();
  expect(detailLine(kokoro)).toBe(
    "1 voice · 337.0 MB download · 0.5 GB memory · Apache 2.0",
  );
  expect(within(modelRow("Qwen3 TTS")).getByText("expressive")).toBeTruthy();
});

const licenceLink = (row: HTMLElement, name: string) =>
  within(row).getByRole("button", { name });

const detailLine = (row: HTMLElement) =>
  row.querySelector(".model__detail")?.textContent;

test("the licence text is one tap from the detail line, and the sheet stays put", async () => {
  await emptyDisk();
  await openSheet();

  fireEvent.click(licenceLink(modelRow("Kokoro"), "Apache 2.0"));

  const licence = await screen.findByRole("dialog", { name: "Apache 2.0" });
  expect(
    within(licence).getByText(/Apache License Version 2.0, January 2004/),
  ).toBeTruthy();
  expect(screen.getByRole("dialog", { name: "Voice models" })).toBeTruthy();

  fireEvent.click(within(licence).getByRole("button", { name: "Done" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Apache 2.0" })).toBe(null),
  );
});

test("a Voice auditions before its model is downloaded, from the app's own bundle", async () => {
  const play = vi
    .spyOn(window.HTMLMediaElement.prototype, "play")
    .mockResolvedValue(undefined);
  const { client, listModels } = fakeClient();
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  await openSheet();
  await waitFor(() => expect(listModels).toHaveBeenCalled());

  fireEvent.click(
    within(modelRow("Kokoro")).getByRole("button", { name: "Hear Heart" }),
  );

  await waitFor(() => expect(play).toHaveBeenCalled());
  const element = play.mock.instances[0] as HTMLAudioElement;
  expect(new URL(element.src).pathname).toBe("/previews/kokoro/82m/af_heart.m4a");
  expect(
    within(modelRow("Kokoro")).getByRole("button", { name: "Stop Heart" }),
  ).toBeTruthy();
  play.mockRestore();
});

test("a Voice with no preview clip yet offers nothing to press", async () => {
  const { client } = fakeClient();
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  await openSheet();

  const row = modelRow("Qwen3 TTS");
  const button = within(row).getByRole("button", {
    name: "Hear Chelsie",
  }) as HTMLButtonElement;

  expect(button.disabled).toBe(true);
  expect(within(row).getAllByText("No preview yet").length).toBeGreaterThan(0);
});

test("downloading runs inline: progress, a verify beat, then usable", async () => {
  const { downloadModel, emitDownload, serveModels } = await emptyDisk();
  await openSheet();

  fireEvent.click(
    within(modelRow("Kokoro")).getByRole("button", {
      name: "Download · 337.0 MB",
    }),
  );
  expect(downloadModel).toHaveBeenCalledWith("kokoro:82m");

  emitDownload(downloadingKokoro());
  expect(screen.getByText("Downloading — 120.0 MB of 337.0 MB")).toBeTruthy();
  expect(screen.getByRole("progressbar")).toBeTruthy();

  emitDownload(downloadingKokoro({ phase: "verifying" }));
  expect(
    screen.getByText("Checking the files match what Readily expects…"),
  ).toBeTruthy();

  serveModels([
    { id: "kokoro:82m", installed: true, diskBytes: KOKORO_BYTES, downloadBytes: KOKORO_BYTES },
    NOTHING_INSTALLED[1],
  ]);
  emitDownload(
    downloadingKokoro({ phase: "installed", bytesDownloaded: KOKORO_BYTES }),
  );

  await waitFor(() =>
    expect(within(modelRow("Kokoro")).getByText("Downloaded")).toBeTruthy(),
  );
});

test("a download that failed mid-flight retries, and says it resumes", async () => {
  const { downloadModel, emitDownload } = await emptyDisk();
  await openSheet();

  emitDownload(failedKokoro());

  expect(
    screen.getByText(
      "The download could not be completed. Trying again picks up where it stopped.",
    ),
  ).toBeTruthy();

  fireEvent.click(
    within(modelRow("Kokoro")).getByRole("button", {
      name: "Download · 337.0 MB",
    }),
  );
  expect(downloadModel).toHaveBeenCalledTimes(1);
  expect(downloadModel).toHaveBeenCalledWith("kokoro:82m");
});

test("progress coming back withdraws the sentence saying it would not", async () => {
  const { emitDownload, loseDownloads } = await emptyDisk();
  await openSheet();

  loseDownloads();
  const lost = "Download progress is unavailable right now. Downloads still run.";
  expect(screen.getByText(lost)).toBeTruthy();

  emitDownload(downloadingKokoro());
  expect(screen.queryByText(lost)).toBe(null);
  expect(screen.getByText("Downloading — 120.0 MB of 337.0 MB")).toBeTruthy();
});

test("a second download waits its turn, even with the sheet closed, and can be taken back out", async () => {
  const { downloadModel, withdrawModel, emitDownload } = await emptyDisk();
  await openSheet();
  emitDownload(downloadingKokoro());

  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: /^Download/ }),
  );
  expect(downloadModel).toHaveBeenCalledWith("qwen3-tts:0.6b");
  emitDownload(
    downloadingKokoro({ queue: [{ modelId: "qwen3-tts:0.6b", action: "download" }] }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Voice models" })).toBe(null),
  );
  await openSheet();

  expect(
    within(modelRow("Qwen3 TTS")).getByText("Waiting to download · #1 in the queue"),
  ).toBeTruthy();
  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Remove from queue" }),
  );
  await waitFor(() => expect(withdrawModel).toHaveBeenCalledWith("qwen3-tts:0.6b"));
});

test("a queue moving straight on to the next download still marks the last one downloaded", async () => {
  const { emitDownload, serveModels } = await emptyDisk();
  await openSheet();
  emitDownload(
    downloadingKokoro({ queue: [{ modelId: "qwen3-tts:0.6b", action: "download" }] }),
  );

  serveModels([BOTH_INSTALLED[0], NOTHING_INSTALLED[1]]);
  emitDownload(
    downloadingKokoro({
      modelId: "qwen3-tts:0.6b",
      bytesTotal: QWEN_BYTES,
      bytesDownloaded: 0,
    }),
  );

  await waitFor(() =>
    expect(within(modelRow("Kokoro")).getByText("Downloaded")).toBeTruthy(),
  );
});

test("a delete asked for during a download waits its turn, then says the disk it freed", async () => {
  const deleteModel = vi.fn<EngineClient["deleteModel"]>(async () => ({
    queued: true,
  }));
  const { client, emitDownload, serveModels, listModels } = fakeClient({
    deleteModel,
  });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  serveModels([NOTHING_INSTALLED[0], BOTH_INSTALLED[1]]);
  await emitDownload(downloadingKokoro({ phase: "idle", bytesDownloaded: 0 }));
  await waitFor(() => expect(listModels).toHaveBeenCalledTimes(2));
  await emitDownload(downloadingKokoro());
  await openSheet();

  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Delete" }),
  );
  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Delete?" }),
  );
  await waitFor(() => expect(deleteModel).toHaveBeenCalledWith("qwen3-tts:0.6b"));
  await emitDownload(
    downloadingKokoro({ queue: [{ modelId: "qwen3-tts:0.6b", action: "delete" }] }),
  );

  expect(
    within(modelRow("Qwen3 TTS")).getByText("Waiting to delete · #1 in the queue"),
  ).toBeTruthy();
  expect(screen.queryByText(/Qwen3 TTS deleted/)).toBe(null);

  serveModels([BOTH_INSTALLED[0], NOTHING_INSTALLED[1]]);
  await emitDownload(
    downloadingKokoro({ phase: "installed", bytesDownloaded: KOKORO_BYTES }),
  );

  await waitFor(() =>
    expect(screen.getByText(/^Qwen3 TTS deleted\. .+ freed\.$/)).toBeTruthy(),
  );
  await waitFor(() =>
    expect(within(modelRow("Qwen3 TTS")).queryByText("Downloaded")).toBe(null),
  );
});

test("a queued delete the Engine could not run is never announced as done", async () => {
  const deleteModel = vi.fn<EngineClient["deleteModel"]>(async () => ({
    queued: true,
  }));
  const { client, emitDownload, serveModels, listModels } = fakeClient({
    deleteModel,
  });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  serveModels([NOTHING_INSTALLED[0], BOTH_INSTALLED[1]]);
  await emitDownload(downloadingKokoro({ phase: "idle", bytesDownloaded: 0 }));
  await waitFor(() => expect(listModels).toHaveBeenCalledTimes(2));
  await emitDownload(downloadingKokoro());
  await openSheet();

  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Delete" }),
  );
  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Delete?" }),
  );
  await waitFor(() => expect(deleteModel).toHaveBeenCalledWith("qwen3-tts:0.6b"));
  await emitDownload(
    downloadingKokoro({ queue: [{ modelId: "qwen3-tts:0.6b", action: "delete" }] }),
  );

  await emitDownload(
    downloadingKokoro({
      phase: "installed",
      bytesDownloaded: KOKORO_BYTES,
      failures: [
        {
          modelId: "qwen3-tts:0.6b",
          error: {
            version: 1,
            code: "store_unwritable",
            message:
              "Readily cannot delete this Voice Model because the folder /models is not writable. Give Readily write access to it.",
          },
        },
      ],
    }),
  );

  await waitFor(() =>
    expect(
      within(modelRow("Qwen3 TTS")).getByText(
        /Trying again works once that folder can be written\./,
      ),
    ).toBeTruthy(),
  );
  expect(within(modelRow("Qwen3 TTS")).getByText("Downloaded")).toBeTruthy();
  expect(screen.queryByText(/Qwen3 TTS deleted/)).toBe(null);
});

test("a queued delete an Engine restart lost is never announced as done", async () => {
  const deleteModel = vi.fn<EngineClient["deleteModel"]>(async () => ({
    queued: true,
  }));
  const { client, connect, emitDownload, serveModels, listModels } = fakeClient({
    deleteModel,
  });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  serveModels([NOTHING_INSTALLED[0], BOTH_INSTALLED[1]]);
  await emitDownload(downloadingKokoro({ phase: "idle", bytesDownloaded: 0 }));
  await waitFor(() => expect(listModels).toHaveBeenCalledTimes(2));
  await emitDownload(downloadingKokoro());
  await openSheet();

  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Delete" }),
  );
  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Delete?" }),
  );
  await waitFor(() => expect(deleteModel).toHaveBeenCalledWith("qwen3-tts:0.6b"));
  await emitDownload(
    downloadingKokoro({ queue: [{ modelId: "qwen3-tts:0.6b", action: "delete" }] }),
  );

  await connect({ state: "starting", detail: "restarting" });
  await connect({ state: "ready" } as Connection);
  await emitDownload(downloadingKokoro({ phase: "idle", bytesDownloaded: 0 }));

  await waitFor(() =>
    expect(within(modelRow("Qwen3 TTS")).getByText("Downloaded")).toBeTruthy(),
  );
  expect(screen.queryByText(/Qwen3 TTS deleted/)).toBe(null);
});

test("a queued delete leaving the queue under an unchanged phase still leaves the listing", async () => {
  const deleteModel = vi.fn<EngineClient["deleteModel"]>(async () => ({
    queued: true,
  }));
  const { client, emitDownload, serveModels, listModels } = fakeClient({
    deleteModel,
    models: BOTH_INSTALLED,
  });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  await emitDownload(
    downloadingKokoro({ phase: "installed", bytesDownloaded: KOKORO_BYTES }),
  );
  await waitFor(() => expect(listModels).toHaveBeenCalledTimes(2));
  await openSheet();

  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Delete" }),
  );
  fireEvent.click(
    within(modelRow("Qwen3 TTS")).getByRole("button", { name: "Delete?" }),
  );
  await waitFor(() => expect(deleteModel).toHaveBeenCalledWith("qwen3-tts:0.6b"));

  // Seen waiting while a delete ahead of it is still being cleaned up,
  // under the phase the last download left.
  await emitDownload(
    downloadingKokoro({
      phase: "installed",
      bytesDownloaded: KOKORO_BYTES,
      queue: [{ modelId: "qwen3-tts:0.6b", action: "delete" }],
    }),
  );
  expect(
    within(modelRow("Qwen3 TTS")).getByText("Waiting to delete · #1 in the queue"),
  ).toBeTruthy();

  // Its turn comes with the phase as it was; only the queue moved.
  serveModels([BOTH_INSTALLED[0], NOTHING_INSTALLED[1]]);
  await emitDownload(
    downloadingKokoro({ phase: "installed", bytesDownloaded: KOKORO_BYTES }),
  );

  await waitFor(() =>
    expect(within(modelRow("Qwen3 TTS")).queryByText("Downloaded")).toBe(null),
  );
  await waitFor(() =>
    expect(screen.getByText(/^Qwen3 TTS deleted\. .+ freed\.$/)).toBeTruthy(),
  );
});

test("deleting a Voice Model arms first, then says the disk it freed", async () => {
  const { client, deleteModel, serveModels } = fakeClient({
    models: [
      {
        id: "kokoro:82m",
        installed: true,
        diskBytes: KOKORO_BYTES,
        downloadBytes: KOKORO_BYTES,
      },
      NOTHING_INSTALLED[1],
    ],
  });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  await openSheet();

  const arm = await waitFor(() =>
    within(modelRow("Kokoro")).getByRole("button", {
      name: "Delete",
    }),
  );
  fireEvent.click(arm);
  expect(deleteModel).not.toHaveBeenCalled();

  serveModels(NOTHING_INSTALLED);
  fireEvent.click(
    within(modelRow("Kokoro")).getByRole("button", { name: "Delete?" }),
  );

  await waitFor(() => expect(deleteModel).toHaveBeenCalledWith("kokoro:82m"));
  await waitFor(() =>
    expect(screen.getByText("Kokoro deleted. 337.0 MB freed.")).toBeTruthy(),
  );
  await waitFor(() =>
    expect(within(modelRow("Kokoro")).queryByText("Downloaded")).toBe(null),
  );
});

test("a Catalog that cannot be read says so instead of reading forever", async () => {
  const { client } = fakeClient({ catalogFails: true });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  await openSheet();

  await waitFor(() =>
    expect(screen.getByText("The Catalog could not be read.")).toBeTruthy(),
  );
  expect(screen.queryByText("Reading the Catalog…")).toBe(null);
});

test("the sheet closes and lets the composer have focus back", async () => {
  const { client } = fakeClient();
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  await openSheet();

  fireEvent.click(screen.getByRole("button", { name: "Done" }));

  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Voice models" })).toBe(null),
  );
  expect(screen.getByRole("textbox", { name: "Source" })).toBeTruthy();
});

const openSettings = async () => {
  fireEvent.click(screen.getByRole("button", { name: "Settings" }));
  return await screen.findByRole("dialog", { name: "Settings" });
};

const shellWithSettings = async (
  overrides: Parameters<typeof fakeClient>[0] = {},
) => {
  const fake = fakeClient(overrides);
  await renderShell(fake.client);
  await openSettings();
  return fake;
};

const budgetMenu = () =>
  screen.getByLabelText("Audio to keep") as HTMLSelectElement;
const keepMenu = () =>
  screen.getByLabelText("Remove audio after") as HTMLSelectElement;

test("the budget is offered as hours of listening rather than as a number of bytes", async () => {
  await shellWithSettings();

  const labels = [...budgetMenu().options].map((option) => option.textContent);
  // How many hours depends on the platform's Segment codec: retention.test.ts.
  expect(labels).toContainEqual(expect.stringMatching(/^5\.0 GB · about \d+ hours$/));
  expect(labels).toContainEqual(expect.stringMatching(/^1\.0 GB · about \d+ hours$/));
});

test("the disk Readily is using is split into voice models and narrated audio", async () => {
  await shellWithSettings();

  expect(
    screen.getByText(
      "5.0 GB of models and audio · 2.0 GB of voice models, 3.0 GB of narrated audio",
    ),
  ).toBeTruthy();
});

test("lowering the budget evicts audio, and the number on screen drops", async () => {
  const { setRetention } = await shellWithSettings();

  fireEvent.change(budgetMenu(), { target: { value: String(GIGABYTE) } });

  await waitFor(() =>
    expect(
      screen.getByText(
        "3.0 GB of models and audio · 2.0 GB of voice models, 1.0 GB of narrated audio",
      ),
    ).toBeTruthy(),
  );
  expect(
    screen.getByText("2.0 GB of audio removed. That disk is free now."),
  ).toBeTruthy();
  expect(setRetention).toHaveBeenCalledWith({
    segmentBudgetBytes: GIGABYTE,
    keepAudioDays: null,
  });
});

test("a keep-audio window is written with the budget it is stored beside", async () => {
  const { setRetention } = await shellWithSettings();

  fireEvent.change(keepMenu(), { target: { value: "30" } });

  await waitFor(() => expect(keepMenu().value).toBe("30"));
  expect(setRetention).toHaveBeenCalledWith({
    segmentBudgetBytes: 5 * GIGABYTE,
    keepAudioDays: 30,
  });
});

test("both retention knobs and the chosen Voice outlive a restart", async () => {
  const fake = await shellWithSettings();

  fireEvent.change(budgetMenu(), { target: { value: String(2 * GIGABYTE) } });
  await waitFor(() => expect(budgetMenu().value).toBe(String(2 * GIGABYTE)));
  fireEvent.change(keepMenu(), { target: { value: "90" } });
  await waitFor(() => expect(keepMenu().value).toBe("90"));
  fireEvent.click(
    within(screen.getByRole("dialog", { name: "Settings" })).getByRole("button", {
      name: "Done",
    }),
  );
  await openPopover();
  fireEvent.click(modelRowIn("Qwen3 TTS"));
  await voiceShown("Qwen3 TTS", "Chelsie");

  cleanup();
  await renderShell(fake.client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  await voiceShown("Qwen3 TTS", "Chelsie");
  await openSettings();

  expect(budgetMenu().value).toBe(String(2 * GIGABYTE));
  expect(keepMenu().value).toBe("90");
});

test("a retention change the Engine refuses leaves the menu showing what is stored", async () => {
  await shellWithSettings({ retentionFails: true });

  fireEvent.change(budgetMenu(), { target: { value: String(GIGABYTE) } });

  await waitFor(() =>
    expect(screen.getByText("Retention could not be saved.")).toBeTruthy(),
  );
  expect(budgetMenu().value).toBe(String(5 * GIGABYTE));
  expect(
    screen.getByText(
      "5.0 GB of models and audio · 2.0 GB of voice models, 3.0 GB of narrated audio",
    ),
  ).toBeTruthy();
});

test("a knob turned while another write is in flight does not carry the old value back", async () => {
  const fake = await shellWithSettings({
    retention: { ...DEFAULT_RETENTION, segmentBudgetBytes: GIGABYTE },
    holdRetention: true,
  });

  fireEvent.change(budgetMenu(), { target: { value: String(5 * GIGABYTE) } });
  fireEvent.change(keepMenu(), { target: { value: "30" } });
  await fake.answerRetention();

  await waitFor(() => expect(keepMenu().value).toBe("30"));
  expect(fake.setRetention).toHaveBeenLastCalledWith({
    segmentBudgetBytes: 5 * GIGABYTE,
    keepAudioDays: 30,
  });
  expect(
    screen.getByText(
      "5.0 GB of models and audio · 2.0 GB of voice models, 3.0 GB of narrated audio",
    ),
  ).toBeTruthy();
});

test("a write the Engine refuses sends the sheet back to what the Engine is enforcing", async () => {
  const fake = await shellWithSettings({ retentionFails: true });
  fake.serveRetention({
    segmentBudgetBytes: GIGABYTE,
    keepAudioDays: null,
    diskUsage: {
      modelsBytes: DEFAULT_RETENTION.diskUsage.modelsBytes,
      audioBytes: GIGABYTE,
    },
  });

  fireEvent.change(budgetMenu(), { target: { value: String(GIGABYTE) } });

  await waitFor(() =>
    expect(screen.getByText("Retention could not be saved.")).toBeTruthy(),
  );
  await waitFor(() => expect(budgetMenu().value).toBe(String(GIGABYTE)));
  expect(
    screen.getByText(
      "3.0 GB of models and audio · 2.0 GB of voice models, 1.0 GB of narrated audio",
    ),
  ).toBeTruthy();
});

const closeSettings = async () => {
  fireEvent.click(
    within(screen.getByRole("dialog", { name: "Settings" })).getByRole("button", {
      name: "Done",
    }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Settings" })).toBe(null),
  );
};

test("a knob turned in a reopened sheet merges with the write the last one left out", async () => {
  const fake = await shellWithSettings({
    retention: { ...DEFAULT_RETENTION, segmentBudgetBytes: GIGABYTE },
    holdRetention: true,
  });

  fireEvent.change(budgetMenu(), { target: { value: String(5 * GIGABYTE) } });
  await closeSettings();
  await openSettings();

  expect(budgetMenu().value).toBe(String(GIGABYTE));
  fireEvent.change(keepMenu(), { target: { value: "30" } });
  await fake.answerRetention();

  await waitFor(() => expect(budgetMenu().value).toBe(String(5 * GIGABYTE)));
  expect(fake.setRetention).toHaveBeenLastCalledWith({
    segmentBudgetBytes: 5 * GIGABYTE,
    keepAudioDays: 30,
  });
  expect(
    screen.getByText(
      "5.0 GB of models and audio · 2.0 GB of voice models, 3.0 GB of narrated audio",
    ),
  ).toBeTruthy();
});

test("a write the Engine never answers does not take Settings down with it", async () => {
  await shellWithSettings({ holdRetention: true });

  fireEvent.change(budgetMenu(), { target: { value: String(GIGABYTE) } });
  await closeSettings();
  await openSettings();

  await waitFor(() => expect(budgetMenu().value).toBe(String(5 * GIGABYTE)));
  expect(
    screen.getByText(
      "5.0 GB of models and audio · 2.0 GB of voice models, 3.0 GB of narrated audio",
    ),
  ).toBeTruthy();
});

test("a sheet left with nothing trustworthy to show gets its knobs back on reopening", async () => {
  const fake = await shellWithSettings({ retentionFails: true });

  fake.failReads(true);
  fireEvent.change(budgetMenu(), { target: { value: String(GIGABYTE) } });

  await waitFor(() =>
    expect(screen.getByText("Retention could not be read.")).toBeTruthy(),
  );
  expect(screen.queryByLabelText("Audio to keep")).toBe(null);

  fake.failReads(false);
  await closeSettings();
  await openSettings();

  expect(budgetMenu().value).toBe(String(5 * GIGABYTE));
});

test("the audio folder is opened by the shell, not by the page", async () => {
  const { openAudioFolder } = await shellWithSettings();

  fireEvent.click(screen.getByRole("button", { name: "Open audio folder" }));

  await waitFor(() => expect(openAudioFolder).toHaveBeenCalledWith());
});

test("the data folder is opened by the shell, not by the page", async () => {
  const { openDataFolder } = await shellWithSettings();

  fireEvent.click(screen.getByRole("button", { name: "Open data folder" }));

  await waitFor(() => expect(openDataFolder).toHaveBeenCalledWith());
});

test("the log file is shown by the shell, not by the page", async () => {
  const { revealLogs } = await shellWithSettings();

  fireEvent.click(screen.getByRole("button", { name: "Show log file" }));

  await waitFor(() => expect(revealLogs).toHaveBeenCalledWith());
});

test("a log that was never written says so rather than doing nothing", async () => {
  // Tauri rejects an `invoke` with the command's `Err` as it was
  // serialised: for `reveal_logs` a plain string, not an `Error`.
  const revealLogs = vi.fn(async () => {
    throw "Readily has not written a log yet.";
  });
  await shellWithSettings({ revealLogs });

  fireEvent.click(screen.getByRole("button", { name: "Show log file" }));

  await waitFor(() =>
    expect(screen.getByText("Readily has not written a log yet.")).toBeTruthy(),
  );
});

test("a data folder that cannot be opened says so rather than doing nothing", async () => {
  const openDataFolder = vi.fn(async () => {
    throw new Error("Readily has not made its data folder yet.");
  });
  await shellWithSettings({ openDataFolder });

  fireEvent.click(screen.getByRole("button", { name: "Open data folder" }));

  await waitFor(() =>
    expect(
      screen.getByText("Readily has not made its data folder yet."),
    ).toBeTruthy(),
  );
});

test("Settings closes without disturbing what the composer was holding", async () => {
  const { client } = fakeClient();
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "The sea was calm." },
  });
  await openSettings();

  fireEvent.click(
    within(screen.getByRole("dialog", { name: "Settings" })).getByRole("button", {
      name: "Done",
    }),
  );

  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Settings" })).toBe(null),
  );
  expect(
    (screen.getByRole("textbox", { name: "Source" }) as HTMLTextAreaElement).value,
  ).toBe("The sea was calm.");
});

test("the row shows the wait for the first words, then only announces what the transport shows", async () => {
  const { client, emit } = fakeClient({ detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(screen.getByText("Engine ready")).toBeTruthy());

  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "One. Two. Three." },
  });
  fireEvent.click(narrateButton());
  await emit({ ...IDLE, phase: "preparing", narrationId: "n-1" });
  await waitFor(() => expect(readAlong()).toBeTruthy());

  const line = () => document.querySelector(".narration__line");
  // The transport is disabled during the wait, so the row shows it. From
  // the first words on, the transport is what a reader sees and the row
  // only announces.
  expect(line()?.textContent).toBe("Getting the words ready…");
  expect(line()?.classList.contains("visually-hidden")).toBe(false);

  await emit({ ...IDLE, phase: "paused", narrationId: "n-1", positionSec: 1, totalSec: 9 });
  await waitFor(() => expect(line()?.textContent).toBe("Paused."));
  expect(line()?.classList.contains("visually-hidden")).toBe(true);

  await emit({ ...IDLE, phase: "finished", narrationId: "n-1", positionSec: 9, totalSec: 9 });
  await waitFor(() => expect(line()?.textContent).toBe("Finished reading."));
  expect(line()?.classList.contains("visually-hidden")).toBe(true);
});

test("a Narration left behind for a stored one is announced, and nothing more", async () => {
  const { client, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: "Second one." }],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));

  fireEvent.click(openRow());
  await emit({ ...IDLE, phase: "finished", narrationId: "n-1", positionSec: 62, totalSec: 62 });
  const line = () => document.querySelector(".narration__line");
  await waitFor(() => expect(line()?.textContent).toBe("Finished reading."));
  expect(line()?.classList.contains("visually-hidden")).toBe(true);

  // A settled Narration is not stopped on the way out, so the reader lands on
  // a stored one with the Engine still remembering the last. That sentence is
  // about a Narration no longer on screen, so it stays out of the reader's
  // way: not shown under the stored document's player, and not a problem
  // that player would show either.
  fireEvent.click(openRow(1));
  await waitFor(() => expect(screen.getByText("Second one.")).toBeTruthy());

  expect(line()?.textContent).toBe("Finished reading.");
  expect(line()?.classList.contains("visually-hidden")).toBe(true);
  expect(document.querySelector(".player__problem")?.textContent).toBe("");
});

test("the title bar names the opened Narration after its Source's first words", async () => {
  const { client } = fakeClient({ history: [ENTRY], detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await waitFor(() => expect(readAlong()).toBeTruthy());

  const heading = screen.getByRole("heading", { level: 1 });
  expect(heading.textContent).toBe("The sea was calm.");
});

test("the title bar stops after six words rather than run the width of the window", async () => {
  const long = { ...ENTRY, sourcePreview: "Hi everyone, thanks for your interest in the accelerator. Our first meeting is Wednesday." };
  const { client } = fakeClient({ history: [long], detail: { ...THREE_BLOCKS, ...long } });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  await waitFor(() => expect(readAlong()).toBeTruthy());

  const heading = screen.getByRole("heading", { level: 1 });
  expect(heading.textContent).toBe("Hi everyone, thanks for your interest…");
});

test("the sidebar folds away and comes back from the title bar, keeping the focus and the way to a new Narration", async () => {
  const { client } = fakeClient({ history: [ENTRY], detail: THREE_BLOCKS });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  const titleBar = () => within(document.querySelector<HTMLElement>(".main__bar")!);
  expect(titleBar().queryByRole("button", { name: "New Narration" })).toBe(null);

  fireEvent.click(screen.getByRole("button", { name: "Hide sidebar" }));
  expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBe(null);
  const show = screen.getByRole("button", { name: "Show sidebar" });
  expect(show.getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(show);
  expect(titleBar().getByRole("button", { name: "New Narration" })).toBeTruthy();

  fireEvent.click(show);
  expect(screen.getByRole("complementary", { name: "Sidebar" })).toBeTruthy();
  const hide = screen.getByRole("button", { name: "Hide sidebar" });
  expect(hide.getAttribute("aria-expanded")).toBe("true");
  expect(document.activeElement).toBe(hide);
  expect(screen.queryByRole("button", { name: "Show sidebar" })).toBe(null);
});

test("the prompt outlives a Narration that finishes while the stop is in flight", async () => {
  let settle = () => {};
  const { client, openNarration, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: "Second one." }],
    detail: THREE_BLOCKS,
    stop: vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    ),
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });

  fireEvent.click(openRow());
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));
  await emit({ ...IDLE, phase: "finished", narrationId: "n-2", positionSec: 9, totalSec: 9 });
  expect(screen.getByText("Still generating")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Cancelling…" })).toBeTruthy();

  settle();
  await waitFor(() => expect(openNarration).toHaveBeenCalledWith("n-1"));
  await waitFor(() => expect(screen.queryByText("Still generating")).toBe(null));
});

test("the prompt cuts a long title between code points", async () => {
  const preview = `${"x".repeat(47)}😀 and more words after`;
  const { client, emit } = fakeClient({
    history: [ENTRY, { ...ENTRY, id: "n-2", sourcePreview: preview }],
    detail: THREE_BLOCKS,
  });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(2));
  await emit({ ...IDLE, phase: "playing", narrationId: "n-2", positionSec: 3, totalSec: 9 });

  fireEvent.click(openRow());
  expect(
    screen.getByText(`Generating “${"x".repeat(47)}😀…” is still in progress. Continuing will cancel it.`),
  ).toBeTruthy();
});

test("Simple offers every Voice and narrates with one no curator has qualified", async () => {
  const { client, narrate } = fakeClient({
    selection: { modelId: "qwen3-tts:0.6b", voiceId: "Chelsie" },
  });
  render(<App client={client} />);
  const pill = await screen.findByRole("button", { name: "Voice Model: Qwen3 TTS" });
  expect(modeSelect().getAttribute("aria-label")).toBe("Reading mode: Simple");
  fireEvent.click(pill);
  expect(within(screen.getByRole("group", { name: "Voice Models" })).getByText("Kokoro")).toBeTruthy();
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.change(screen.getByRole("textbox", { name: "Source" }), {
    target: { value: "A local Narration." },
  });
  await waitFor(() => expect(narrateButton().disabled).toBe(false));
  fireEvent.click(narrateButton());
  await waitFor(() => expect(narrate).toHaveBeenCalledWith("A local Narration.", {
    modelId: "qwen3-tts:0.6b", voiceId: "Chelsie",
  }, "simple"));
});


test("transport skips exact seconds and clamps at the Narration boundaries", async () => {
  const { client, seekTime, seek, emit } = fakeClient({ history: [ENTRY] });
  await renderShell(client);
  await waitFor(() => expect(historyRows()).toHaveLength(1));
  fireEvent.click(openRow());
  const update = (positionSec: number) => emit({ ...IDLE, phase: "playing", narrationId: "n-1", positionSec, totalSec: 62 });
  await update(20);
  fireEvent.click(screen.getByRole("button", { name: "Back 15 seconds" }));
  expect(seekTime).toHaveBeenLastCalledWith(5);
  fireEvent.click(screen.getByRole("button", { name: "Forward 30 seconds" }));
  expect(seekTime).toHaveBeenLastCalledWith(50);
  await update(5);
  fireEvent.click(screen.getByRole("button", { name: "Back 15 seconds" }));
  expect(seekTime).toHaveBeenLastCalledWith(0);
  await update(55);
  fireEvent.click(screen.getByRole("button", { name: "Forward 30 seconds" }));
  expect(seekTime).toHaveBeenLastCalledWith(62);
  await update(0);
  expect(screen.getByRole("button", { name: "Back 15 seconds" }).hasAttribute("disabled")).toBe(true);
  await update(62);
  expect(screen.getByRole("button", { name: "Forward 30 seconds" }).hasAttribute("disabled")).toBe(true);
  await emit({ ...IDLE });
  expect(screen.getByRole("button", { name: "Back 15 seconds" }).hasAttribute("disabled")).toBe(true);
  expect(screen.getByRole("button", { name: "Forward 30 seconds" }).hasAttribute("disabled")).toBe(true);
  expect(seek).not.toHaveBeenCalled();
});

test("a newer Readily is offered to the reader rather than installed behind them", async () => {
  const fake = fakeClient({
    update: { state: "available", version: "0.2.0", notes: "Faster Narration." },
  });
  await renderShell(fake.client);

  await screen.findByRole("dialog", { name: "Readily 0.2.0 is ready" });
  expect(screen.getByText("Faster Narration.")).toBeTruthy();
  expect(fake.installUpdate).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Install and restart" }));

  await waitFor(() => expect(fake.installUpdate).toHaveBeenCalledWith());
});

test("a reader still waiting on the Engine is asked about the Engine, not a version", async () => {
  const fake = fakeClient({
    connection: { state: "starting", detail: "provisioning", note: null },
    models: NOTHING_INSTALLED,
    holdModels: true,
    update: { state: "available", version: "0.2.0", notes: null },
  });
  render(<App client={fake.client} />);

  await waitFor(() => expect(fake.updateStatus).toHaveBeenCalled());
  // The first-run screen is what is on the glass, and it has no dialog over
  // it: the offer waits for a Readily the reader can actually use.
  expect(screen.getByRole("main", { name: "Setting up Readily" })).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBe(null);
});

test("a reader who says later is not asked again for the rest of the run", async () => {
  const fake = fakeClient({
    update: { state: "available", version: "0.2.0", notes: null },
  });
  await renderShell(fake.client);

  await screen.findByRole("dialog", { name: "Readily 0.2.0 is ready" });
  fireEvent.click(screen.getByRole("button", { name: "Later" }));

  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Readily 0.2.0 is ready" })).toBe(null),
  );
});
