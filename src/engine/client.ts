import { controlSettings, isControlSchema, isDiagnostics } from "./advanced";
import type { Control, ControlSettings, Diagnostics, Overrides, TakeAction } from "./advanced";

export type EngineConfig = { port: number; token: string };

export type EngineStatus =
  | { state: "provisioning"; note: string | null }
  | { state: "starting"; attempt: number }
  | { state: "ready"; port: number }
  | { state: "restarting"; attempt: number; retryInMs: number }
  | { state: "failed"; reason: string };

// Whether a newer Readily is published, and how an install is going. Read
// by polling `update_status`, the same way the Engine's own status is: the
// shell subscribes to nothing, so a screen that mounts late misses nothing.
// A check that could not be made reads as `idle` — the reader never asked
// for it, so a network that was down has nothing to say to them.
export type UpdateStatus =
  | { state: "idle" }
  | { state: "available"; version: string; notes: string | null }
  | { state: "installing" }
  // `untouched`: whether this build is still the one on disk. False when
  // the swap itself failed, which can leave no Readily to relaunch.
  | { state: "failed"; reason: string; untouched: boolean };

// What the shell shows about reaching the Engine. `detail` splits the
// supervisor's pre-ready states into the three waits that feel different to
// a reader: the first-run environment build, an ordinary launch, and a
// recovery after the Engine died. The port and attempt counts stay behind
// this boundary — they are the supervisor's business, not a screen's.
export type Connection =
  // The first-run environment build, and the last line `uv` wrote — `null`
  // before it has written one. The note rides on this variant alone
  // because it is the only wait long enough to need proof it is moving.
  | { state: "starting"; detail: "provisioning"; note: string | null }
  | { state: "starting"; detail: "launching" | "restarting" }
  | { state: "ready" }
  | { state: "failed"; message: string };

// Every phase the v1 `/v1/events` snapshot can carry (docs/wire.md). A
// phase left out here is not merely unrendered: `isNarrationState` rejects
// the snapshot carrying it, which drops the stream and reconnects into the
// same snapshot again.
export type NarrationPhase =
  | "idle"
  | "preparing"
  | "playing"
  | "paused"
  | "finished"
  | "failed";

export type WireError = {
  version: 1;
  code: string;
  message: string;
};

export type NarrationState = {
  version: 1;
  phase: NarrationPhase;
  narrationId: string | null;
  // Whichever Catalog entry the Engine resolved — the client does not pick
  // the model, so it cannot name the ones that exist either.
  modelId: string;
  voiceId: string;
  positionSec: number;
  // How loud what the device is playing right now is, 0–1. Live like
  // `positionSec`; it is what the voice orb breathes with.
  level: number;
  totalSec: number;
  speed: number;
  generationBehind: boolean;
  diagnostics: Diagnostics;
  lateCallbacks: number;
  error: WireError | null;
};

// A stored Narration's own lifecycle, which is not the active Narration's
// phase: a restarted Engine rewrites leftover `preparing`/`playing` rows to
// `interrupted`, and `stopped` is a Narration the reader ended themselves.
export type NarrationStatusName =
  | "preparing"
  | "playing"
  | "interrupted"
  | "stopped"
  | "finished"
  | "failed";

// One History row on the wire. `sourcePreview` is the first 160 characters
// of the stored Source — History responses never carry a Segment hash, a
// database key, or a filesystem path.
export type HistoryEntry = {
  id: string;
  sourcePreview: string;
  modelId: string;
  voiceId: string;
  speed: number;
  status: NarrationStatusName;
  createdAt: string;
  updatedAt: string;
  lastPlayedAt: string;
  playheadSec: number;
  // `null` until the Narration finishes.
  totalDurationSec: number | null;
  // True only while every manifested Segment still has a file: retention
  // sweeps audio out from under a row that stays forever (ADR 0004 §5).
  audioPresent: boolean;
  // True while any Block recorded a durable failure this Narration has not
  // since made good (ADR 0002 §8). Replaying it can retract the claim.
  hasGaps: boolean;
};

export type SourceTiming = {
  sourceStart: number;
  sourceEnd: number;
  startSec: number;
  endSec: number;
  provenance: "spoken" | "matched" | "estimated";
};

export type HistorySegment = {
  ordinal: number;
  sourceStart: number;
  sourceEnd: number;
  boundary: "sentence" | "paragraph" | "mid-sentence";
  // `null` for a Block whose length the Engine does not know yet.
  durationSec: number | null;
  audioPresent: boolean;
  // Derived from trimmed Block lengths and the Narration's pause policy;
  // null when an earlier length is unknown. Used for read-along highlighting.
  timelineStartSec: number | null;
  // Only reliable words, placed on this Narration’s Source and timeline.
  timings: SourceTiming[];
};

// A Block this Narration failed to read, indexed against its own Source.
export type HistoryGap = {
  ordinal: number;
  sourceStart: number;
  sourceEnd: number;
  errorCode: string;
  createdAt: string;
};

export type HistoryNarration = HistoryEntry & {
  source: string;
  segments: HistorySegment[];
  gaps: HistoryGap[];
};

// A Narration read past a cursor: only the Segments after it, and no
// Source. The reader of the earlier full read keeps both.
export type HistoryNarrationAfter = Omit<HistoryNarration, "source">;

export type Deletion = {
  narrationId: string;
  // What the Engine's refcount sweep actually reclaimed. Zero when the
  // audio was already evicted, or when its Segments are shared.
  audioBytesFreed: number;
};

// One Voice as the Catalog describes it, before anything is downloaded.
export type Mode = "simple" | "advanced";

export type CatalogVoice = {
  // Whether this Voice's current recipe has been qualified, which is what
  // makes it one Simple mode may offer.
  simple: boolean;
  id: string;
  name: string;
  language: string;
  // Where this Voice's bundled Voice Preview clip lives, relative to the
  // app's own preview root, or `null` for a Voice curation has not made a
  // clip for yet. The clip is an app resource: auditioning one is a bundle
  // read, never an Engine call and never a network call.
  preview: string | null;
};

// The licence an entry's weights are under, as the sheet shows it (ADR
// 0009 §3-4): the full text one tap away, whether downloading accepts it,
// and the facts CC-BY §3(a)(1) asks an attribution to carry.
export type CatalogLicense = {
  id: string;
  name: string;
  // Whether the licence binds the reader, not only Readily. When true the
  // download is the acceptance and the sheet says so.
  bindsReader: boolean;
  // A line the licence asks the UI to display ("Built with Llama"), or null.
  credit: string | null;
  // Where the weights came from, for a licence that asks the attribution to
  // travel with the terms (CC-BY §3(a)(1)), and `null` for one that does
  // not. Which licences ask is the Engine's decision (docs/wire.md); the
  // shell shows the block when it is here and reads no licence law.
  attribution: {
    creator: string;
    copyrightNotice: string;
    // The pinned upstream revision, as a page a reader can open.
    source: string;
    warrantyNotice: string;
    // Whether Readily changed the files it hands out.
    modified: boolean;
  } | null;
  text: string;
};

// One Catalog entry as the sheet renders it: what a reader chooses between,
// with none of the curation facts the Engine fetches and runs with.
export type CatalogEntry = {
  id: string;
  name: string;
  // The Manifest's own Tier value, carried through verbatim — the shell
  // never invents a second speed vocabulary on top of it (CONTEXT.md).
  tier: string;
  // Beside v1's original `license` id string, which the shell no longer
  // reads (docs/wire.md: a v1 shape only grows).
  licenseTerms: CatalogLicense;
  supportModels: { name: string; licenseTerms: CatalogLicense }[];
  parameters?: Record<string, Control>;
  wordTimingModels?: { id: string; name: string }[];
  // Roughly what running this entry costs in memory. A class, not a
  // measurement: it is shown, and it never hides an entry.
  ramClassGb: number;
  voices: CatalogVoice[];
  defaultVoiceId: string;
  downloadBytes: number;
};

// The Voice a Narration is made with: a Catalog entry and one of the
// Voices it offers. The Engine resolves it against the baked Manifest
// before answering, so it always names a Voice Model the Catalog still has
// — the shell renders it without a fallback of its own.
export type VoiceSelection = {
  modelId: string;
  voiceId: string;
};

export type Catalog = {
  defaultModelId: string;
  models: CatalogEntry[];
};

// What the Engine's janitor is currently holding, and how much room it is
// taking. `modelsBytes` is outside the budget entirely — Voice Models have
// their own lifecycle (ADR 0003) — and is here because a reader checking
// "why is Readily 4 GB?" is owed both halves of the answer.
export type DiskUsage = {
  modelsBytes: number;
  audioBytes: number;
};

// The two knobs of ADR 0004 §5. They travel together everywhere, because
// the Engine takes them as a pair: a policy is both answers or neither.
//
// `keepAudioDays` of `null` is Never — the clock is off, and the budget is
// the only thing that removes audio.
export type RetentionPolicy = {
  segmentBudgetBytes: number;
  keepAudioDays: number | null;
};

// A stored policy, read back with the disk it governs. `audioBytes` can sit
// above the budget: it is what is on disk right now, not what the policy
// will settle at.
export type RetentionSettings = RetentionPolicy & {
  diskUsage: DiskUsage;
};

// A stored policy read back with what applying it just removed. Only a write
// has that number, and only the Engine can produce it: its own retention
// schedule sweeps between any two disk readings, so a shell subtracting one
// from the other would credit the janitor's work to whatever the reader last
// pressed.
export type RetentionApplied = RetentionSettings & {
  evictedBytes: number;
};

// What the local model store holds for one Catalog entry. `installed` is
// the store's whole invariant: verified and promoted, or absent (ADR 0003).
export type ModelStatus = {
  id: string;
  installed: boolean;
  diskBytes: number;
  downloadBytes: number;
};

export type DownloadPhase =
  | "idle"
  | "downloading"
  | "verifying"
  | "installed"
  | "failed";

// The Engine's snapshot of the one download it runs at a time.
export type DownloadState = {
  version: 1;
  phase: DownloadPhase;
  modelId: string | null;
  bytesTotal: number;
  bytesDownloaded: number;
  error: WireError | null;
};

// M4A (AAC) by default, WAV as the lossless option. No MP3 — ADR 0004 §4.
export type ExportFormat = "m4a" | "wav";

export type ExportOptions = {
  format?: ExportFormat;
  // What the save panel offers as the file name before the user edits it.
  defaultName?: string;
};

export type SavePanel = (options: {
  defaultPath?: string;
  filters?: { name: string; extensions: string[] }[];
}) => Promise<string | null>;

export type WatchHandlers = {
  onConnection?: (connection: Connection) => void;
  onNarration?: (state: NarrationState) => void;
};

export type DownloadHandlers = {
  onDownload?: (state: DownloadState) => void;
  // The stream could not be reached, and progress will not arrive until it
  // can be. Called once per run of failures rather than once per retry —
  // the loop retries twice a second, and the screen only needs telling
  // that what it is showing has stopped being live.
  onLost?: () => void;
};

export type EngineClient = {
  watch: (handlers: WatchHandlers, signal: AbortSignal) => Promise<void>;
  // Ask the supervisor to start the Engine over after it gave up. The only
  // way back from a failed connection, and the reason a first run whose
  // network dropped is a retry rather than a dead app. Resolves once the
  // supervisor has been asked — what happens next arrives on `watch`.
  retryEngine: () => Promise<void>;
  // Narrate `input` with `voice`, or with whatever the Engine resolves as
  // its default when the shell has not read a selection yet.
  narrate: (
    input: string,
    voice: VoiceSelection | undefined,
    mode: Mode,
  ) => Promise<void>;
  stop: () => Promise<void>;
  // Freeze the active Narration's playhead, and release it exactly where it
  // froze. Neither is a refusal in the wrong phase — the Engine answers
  // `false` and changes nothing — so the shell offers them from the phase it
  // is already mirroring rather than guarding them twice.
  pause: () => Promise<void>;
  play: () => Promise<void>;
  // Seek to the Block containing this Unicode code-point Source offset.
  seek: (sourceOffset: number) => Promise<void>;
  seekTime: (positionSec: number) => Promise<void>;
  // Change playback speed without regenerating or replacing the Narration.
  setSpeed: (speed: number) => Promise<void>;
  controls: (voice: VoiceSelection) => Promise<ControlSettings>;
  setControls: (voice: VoiceSelection, overrides: Overrides) => Promise<ControlSettings>;
  selectTake: (narrationId: string, ordinal: number, action: TakeAction) => Promise<void>;
  // History, newest first. The Engine owns the order (created, then id,
  // both descending) and the shell does not re-sort it.
  listHistory: () => Promise<HistoryEntry[]>;
  // One Narration with its full Source, Segment ranges and gap records.
  // With `afterOrdinal`, only the Segments past that ordinal and no Source.
  openNarration: {
    (narrationId: string): Promise<HistoryNarration>;
    (narrationId: string, options: { afterOrdinal: number }): Promise<HistoryNarrationAfter>;
  };
  // Make a stored Narration the active one again: from its playhead if it
  // was interrupted or stopped, from the start if it finished. `paused`
  // holds it there silently until `play`. Accepted means queued — progress
  // arrives on the same `/v1/events` stream as everything else.
  resumeNarration: (
    narrationId: string,
    mode: Mode,
    options?: { paused: boolean },
  ) => Promise<void>;
  // Permanent, and the only way a History entry ever goes away.
  deleteNarration: (narrationId: string) => Promise<Deletion>;
  exportNarration: (
    narrationId: string,
    options?: ExportOptions,
  ) => Promise<boolean>;
  // The whole Catalog, downloaded or not. Baked into the release, so this
  // answers with the network off and nothing on disk (ADR 0003 §1).
  listCatalog: () => Promise<Catalog>;
  // What the model store actually holds, keyed by Catalog id.
  listModels: () => Promise<ModelStatus[]>;
  // Start or resume a download. Accepted means queued: progress arrives on
  // `watchDownloads`, and asking again after a failure resumes from the
  // bytes already staged rather than starting over.
  downloadModel: (modelId: string) => Promise<void>;
  // Remove a Voice Model's files. Idempotent; refused while its own
  // download is running.
  deleteModel: (modelId: string) => Promise<void>;
  // The Voice the composer shows as current: the stored choice, resolved
  // against the Catalog. Answers before anything has been chosen and
  // before anything is downloaded.
  voiceSelection: () => Promise<VoiceSelection>;
  // Persist a Voice choice made without narrating — the only way one
  // survives a quit, since the Engine otherwise records it as a side
  // effect of starting a Narration. Answers with what is now stored.
  selectVoice: (selection: VoiceSelection) => Promise<VoiceSelection>;
  // The two retention knobs and the disk they govern (ADR 0004 §5).
  retention: () => Promise<RetentionSettings>;
  // Write both knobs — the Engine takes them as a pair, and answers with
  // the disk usage *after* the new policy has been applied, plus the bytes
  // applying it removed. That reply is what lets a lowered budget show its
  // eviction as a measured number rather than as a claim that it will.
  setRetention: (chosen: RetentionPolicy) => Promise<RetentionApplied>;
  // Show the data folder in the Finder. Goes to the supervisor rather than
  // over the wire: the folder is the Rust side's own idea of where it put
  // things, and the command takes no arguments, so nothing the webview
  // holds can steer what gets opened (threat model B4).
  openDataFolder: () => Promise<void>;
  // Show the log file in the Finder: the one file a reader is asked to
  // send with a bug report. Same shape as `openDataFolder`, for the same
  // reason: the path is the Rust side's own, and nothing crosses.
  revealLogs: () => Promise<void>;
  // Whether a newer Readily is published. Goes to the supervisor, not over
  // the wire: the update check is the Rust side's network call, made
  // against the endpoint and the signing key this build was compiled with.
  updateStatus: () => Promise<UpdateStatus>;
  // Take the update that was offered. Argument-free like the rest, so no
  // version, URL or host crosses the boundary to say which release to
  // install (threat model B4, B6). Readily restarts itself when it lands,
  // so this resolving means only that the install has started.
  installUpdate: () => Promise<void>;
  // Download progress, for as long as the caller keeps the signal open.
  // Separate from `watch` because it is a second stream with a second
  // lifetime — the sheet that shows it is not always on screen.
  watchDownloads: (
    handlers: DownloadHandlers,
    signal: AbortSignal,
  ) => Promise<void>;
};

type Dependencies = {
  invoke: (command: string) => Promise<unknown>;
  fetch?: typeof fetch;
  delay?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
  // The shell's native save panel. Injected rather than imported so the
  // client tests without a Tauri host, and so `dialog:allow-save` has
  // exactly one call site.
  save?: SavePanel;
};

// The signal outlives every individual delay — it is the watch loop's, and
// the loop delays several times a second while the Engine provisions — so
// the listener has to come off on the timer's path too, not only on abort.
const defaultDelay = (milliseconds: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    if (signal.aborted) return resolve();
    const settle = () => {
      window.clearTimeout(handle);
      signal.removeEventListener("abort", settle);
      resolve();
    };
    const handle = window.setTimeout(settle, milliseconds);
    signal.addEventListener("abort", settle);
  });

// `provisioning` is its own beat because it is the multi-minute one; a
// `restarting` supervisor is retrying after a death, which is worth saying
// out loud too. Everything else on the way up is an ordinary launch.
const starting = (status: EngineStatus): Connection => {
  if (status.state === "provisioning") {
    return { state: "starting", detail: "provisioning", note: status.note };
  }
  const detail = status.state === "restarting" ? "restarting" : "launching";
  return { state: "starting", detail };
};

const isAttribution = (value: unknown): value is CatalogLicense["attribution"] => {
  if (value === null) return true;
  if (typeof value !== "object") return false;
  return (
    "creator" in value &&
    typeof value.creator === "string" &&
    "copyrightNotice" in value &&
    typeof value.copyrightNotice === "string" &&
    "source" in value &&
    typeof value.source === "string" &&
    "warrantyNotice" in value &&
    typeof value.warrantyNotice === "string" &&
    "modified" in value &&
    typeof value.modified === "boolean"
  );
};

const isCatalogLicense = (value: unknown): value is CatalogLicense => {
  if (typeof value !== "object" || value === null) return false;
  const terms = value as Partial<CatalogLicense>;
  return (
    typeof terms.id === "string" &&
    typeof terms.name === "string" &&
    typeof terms.bindsReader === "boolean" &&
    (terms.credit === null || typeof terms.credit === "string") &&
    isAttribution(terms.attribution) &&
    typeof terms.text === "string"
  );
};

// V1 Catalog shapes only grow, but the shell must refuse an older or malformed
// entry when it would leave a required client fact undefined.
const carriesCatalogContract = (entry: unknown) => {
  if (typeof entry !== "object" || entry === null) return false;
  const candidate = entry as Partial<CatalogEntry>;
  return (
    (candidate.parameters === undefined || isControlSchema(candidate.parameters)) &&
    (candidate.wordTimingModels === undefined || (Array.isArray(candidate.wordTimingModels) && candidate.wordTimingModels.every(
      (model: unknown) => typeof model === "object" && model !== null && "id" in model && typeof model.id === "string" && "name" in model && typeof model.name === "string"
    ))) &&
    isCatalogLicense(candidate.licenseTerms) &&
    Array.isArray(candidate.voices) &&
    candidate.voices.every((voice: unknown) =>
      typeof voice === "object" && voice !== null &&
      "simple" in voice && typeof voice.simple === "boolean"
    ) &&
    Array.isArray(candidate.supportModels) &&
    candidate.supportModels.every((support: unknown) =>
      typeof support === "object" && support !== null &&
      "name" in support && typeof support.name === "string" &&
      "licenseTerms" in support && isCatalogLicense(support.licenseTerms)
    )
  );
};

const isDownloadState = (value: unknown): value is DownloadState => {
  if (typeof value !== "object" || value === null) return false;
  const state = value as Partial<DownloadState>;
  return (
    state.version === 1 &&
    ["idle", "downloading", "verifying", "installed", "failed"].includes(
      state.phase ?? "",
    )
  );
};

const isNarrationState = (value: unknown): value is NarrationState => {
  if (typeof value !== "object" || value === null) return false;
  const state = value as Partial<NarrationState>;
  return (
    state.version === 1 &&
    isDiagnostics(state.diagnostics) &&
    typeof state.speed === "number" &&
    Number.isFinite(state.speed) &&
    typeof state.generationBehind === "boolean" &&
    ["idle", "preparing", "playing", "paused", "finished", "failed"].includes(
      state.phase ?? "",
    )
  );
};

// Reads one SSE stream to its end, handing every frame of the named event
// to `on`. The event name and the guard are parameters because the Engine
// has two streams of this shape — Narration and download — and they differ
// in exactly those two things.
const readSse = async <T>(
  response: Response,
  event: string,
  accepts: (value: unknown) => value is T,
  on: ((snapshot: T) => void) | undefined,
) => {
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new Error("The Engine event stream has no body.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatch = (frame: string) => {
    const lines = frame.split(/\r?\n/);
    const named = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
    if (named !== event) return;
    const data = lines
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    const snapshot: unknown = JSON.parse(data);
    if (!accepts(snapshot)) {
      throw new Error("The Engine sent an unsupported event version.");
    }
    on?.(snapshot);
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() ?? "";
      for (const frame of frames) dispatch(frame);
      // Whatever is left at EOF is a frame the blank line never terminated:
      // a stream cut mid-event. Parsing it would surface a JSON error where
      // the truth is a dropped connection, which the caller retries.
      if (done) break;
    }
  } finally {
    // A frame that fails to parse must not leave the body locked to a
    // reader nobody holds: the watch loop reconnects every 250ms, and each
    // abandoned reader keeps its connection — and the Engine's generator —
    // alive behind it.
    await reader.cancel().catch(() => {});
  }
};

const POST_TIMEOUT_MS = 10_000;

const responseError = async (response: Response) => {
  try {
    const body = (await response.json()) as { error?: WireError };
    if (body.error?.version === 1) return new Error(body.error.message);
  } catch {
    // Fall through to the status-only message when the body is not v1 JSON.
  }
  return new Error(`The Engine request failed (${response.status}).`);
};

export const createEngineClient = ({
  invoke,
  fetch: fetcher = fetch,
  delay = defaultDelay,
  save,
}: Dependencies): EngineClient => {
  let config: EngineConfig | null = null;

  // StrictMode mounts the watch loop twice. The first loop keeps running
  // until its in-flight fetch unwinds, and its parting `config = null`
  // would otherwise strand the live loop's narrate and stop calls against
  // an Engine that is up. An aborted loop publishes nothing.
  const publish = (next: EngineConfig | null, signal: AbortSignal) => {
    if (!signal.aborted) config = next;
  };

  const endpoint = () => {
    if (!config) throw new Error("The Engine is not ready.");
    return {
      url: `http://127.0.0.1:${config.port}`,
      headers: { Authorization: `Bearer ${config.token}` },
    };
  };

  // Every POST is an acknowledgement the Engine answers at once, so one
  // that has not answered in this long is wedged.
  const post = async (path: string, body?: object) => {
    const engine = endpoint();
    const deadline = new AbortController();
    const timer = setTimeout(() => deadline.abort(), POST_TIMEOUT_MS);
    try {
      const response = await fetcher(`${engine.url}${path}`, {
        method: "POST",
        headers: body
          ? { ...engine.headers, "Content-Type": "application/json" }
          : engine.headers,
        body: body ? JSON.stringify(body) : undefined,
        signal: deadline.signal,
      }).catch((error: unknown) => {
        if (deadline.signal.aborted) throw new Error("The Engine did not answer.");
        throw error;
      });
      if (!response.ok) throw await responseError(response);
    } finally {
      clearTimeout(timer);
    }
  };

  // Every reply the shell reads is version-gated the way the event stream
  // is: a shell that rendered a shape it did not recognise would be
  // inventing the parts it failed to understand. `post` has nothing to gate
  // — the Engine's `202` accepted shapes are acknowledgements the shell
  // reads no field out of, and what happens next arrives on `/v1/events`.
  // Past the gate the payload is taken at its word — `docs/wire.md` is the
  // contract, and this is the one place the wire becomes types.
  const readVersioned = async <T>(response: Response): Promise<T> => {
    if (!response.ok) throw await responseError(response);
    const body: unknown = await response.json();
    if ((body as { version?: unknown } | null)?.version !== 1) {
      throw new Error("The Engine sent a reply this app cannot read.");
    }
    return body as T;
  };

  const request = async <T>(path: string, method: "GET" | "DELETE") => {
    const engine = endpoint();
    return readVersioned<T>(
      await fetcher(`${engine.url}${path}`, { method, headers: engine.headers }),
    );
  };

  const patch = async <T>(path: string, body: object) => {
    const engine = endpoint();
    return readVersioned<T>(
      await fetcher(`${engine.url}${path}`, {
        method: "PATCH",
        headers: { ...engine.headers, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  };

  // A selection the shell can render, or nothing at all. Past the version
  // gate the wire is taken at its word everywhere else, but this one is
  // rendered as a sentence in the composer's pill, and two `undefined`s
  // either side of a separator is not a Voice.
  const selection = (body: Partial<VoiceSelection>): VoiceSelection => {
    if (typeof body.modelId !== "string" || typeof body.voiceId !== "string") {
      throw new Error("The Engine named a Voice this app cannot read.");
    }
    return { modelId: body.modelId, voiceId: body.voiceId };
  };

  // Retention is rendered as numbers — a budget, a window, two disk
  // figures — and numbers are the one thing a missing field turns into
  // nonsense rather than into nothing. `NaN GB on disk` would be the shell
  // inventing the part it failed to understand, so the same gate the Voice
  // selection gets applies here.
  const settings = (body: Partial<RetentionSettings>): RetentionSettings => {
    const { segmentBudgetBytes, keepAudioDays, diskUsage } = body;
    const readable =
      typeof segmentBudgetBytes === "number" &&
      (keepAudioDays === null || typeof keepAudioDays === "number") &&
      typeof diskUsage?.modelsBytes === "number" &&
      typeof diskUsage.audioBytes === "number";
    if (!readable) {
      throw new Error("The Engine sent retention settings this app cannot read.");
    }
    return {
      segmentBudgetBytes,
      keepAudioDays: keepAudioDays ?? null,
      diskUsage: {
        modelsBytes: diskUsage.modelsBytes,
        audioBytes: diskUsage.audioBytes,
      },
    };
  };

  const route = (narrationId: string) =>
    `/v1/history/${encodeURIComponent(narrationId)}`;

  function openNarration(narrationId: string): Promise<HistoryNarration>;
  function openNarration(
    narrationId: string,
    options: { afterOrdinal: number },
  ): Promise<HistoryNarrationAfter>;
  function openNarration(narrationId: string, options?: { afterOrdinal: number }) {
    const cursor = options ? `?afterOrdinal=${options.afterOrdinal}` : "";
    return request<HistoryNarrationAfter>(`${route(narrationId)}${cursor}`, "GET");
  }

  return {
    async watch(handlers, signal) {
      // Nothing reaches a watcher whose signal has aborted. Every branch
      // below tells the handlers something after an `await`, and the one
      // that resolves last on a torn-down effect is React's StrictMode
      // double-mount handing a dead consumer a connection state.
      const say = (connection: Connection) => {
        if (!signal.aborted) handlers.onConnection?.(connection);
      };
      const narration = (state: NarrationState) => {
        if (!signal.aborted) handlers.onNarration?.(state);
      };

      // The supervisor's failure, once it has been passed on. A failed
      // Engine is no longer the end of the loop — it can be retried, and
      // the way back up arrives as an ordinary status — so the poll carries
      // on. Announcing the same dead Engine four times a second would
      // re-render every screen watching it for as long as it stays dead.
      //
      // Per watcher rather than per client: two watchers are two loops with
      // two consumers, and one of them having already said this tells the
      // other nothing.
      let announced: string | null = null;

      while (!signal.aborted) {
        try {
          const status = (await invoke("engine_status")) as EngineStatus;
          if (status.state === "failed") {
            publish(null, signal);
            if (announced !== status.reason) {
              announced = status.reason;
              say({ state: "failed", message: status.reason });
            }
            await delay(250, signal);
            continue;
          }
          announced = null;
          if (status.state !== "ready") {
            publish(null, signal);
            say(starting(status));
            await delay(250, signal);
            continue;
          }

          const ready = (await invoke("engine_config")) as EngineConfig | null;
          publish(ready, signal);
          if (!ready) {
            say({ state: "starting", detail: "launching" });
            await delay(100, signal);
            continue;
          }
          say({ state: "ready" });
          const engine = endpoint();
          const response = await fetcher(`${engine.url}/v1/events`, {
            headers: engine.headers,
            signal,
          });
          await readSse(response, "narration", isNarrationState, narration);
        } catch (error) {
          if (signal.aborted) return;
          publish(null, signal);
          announced = null;
          say({
            state: "failed",
            message: error instanceof Error ? error.message : "The Engine disconnected.",
          });
        }
        if (!signal.aborted) {
          publish(null, signal);
          await delay(250, signal);
        }
      }
    },

    async retryEngine() {
      await invoke("engine_retry");
    },

    async watchDownloads(handlers, signal) {
      // Its own reconnecting loop rather than a second stream inside
      // `watch`: this one is opened and closed by a screen, and it must be
      // able to come and go without disturbing the Narration stream the
      // whole app depends on. `endpoint()` throws until `watch` has
      // published a config, which the catch below treats like any other
      // reason to wait and try again.
      let announced = false;
      // A snapshot is the one proof the stream is live again, so it is what
      // clears the latch. Clearing it on the response instead would take an
      // Engine that answers every connect with a `401` — `readSse` rejects
      // on a response that is not ok — and announce the same loss twice a
      // second for as long as it lasted.
      const download = (state: DownloadState) => {
        announced = false;
        if (!signal.aborted) handlers.onDownload?.(state);
      };
      while (!signal.aborted) {
        try {
          const engine = endpoint();
          const response = await fetcher(`${engine.url}/v1/models/events`, {
            headers: engine.headers,
            signal,
          });
          await readSse(response, "download", isDownloadState, download);
        } catch {
          if (signal.aborted) return;
          if (!announced) {
            announced = true;
            handlers.onLost?.();
          }
        }
        // A dropped stream loses nothing: the Engine's snapshot is the whole
        // download state and it is re-sent on connect.
        if (!signal.aborted) await delay(500, signal);
      }
    },

    async listCatalog() {
      const body = await request<{ models?: unknown }>("/v1/catalog", "GET");
      if (
        !Array.isArray(body.models) ||
        !body.models.every(carriesCatalogContract)
      ) {
        throw new Error("The Engine sent a Catalog this app cannot read.");
      }
      return body as Catalog;
    },

    async listModels() {
      const body = await request<{ models?: unknown }>("/v1/models", "GET");
      if (!Array.isArray(body.models)) {
        throw new Error("The Engine sent a model list this app cannot read.");
      }
      return body.models as ModelStatus[];
    },

    async voiceSelection() {
      return selection(await request<Partial<VoiceSelection>>(
        "/v1/settings/voice",
        "GET",
      ));
    },

    async selectVoice(chosen) {
      return selection(
        await patch<Partial<VoiceSelection>>("/v1/settings/voice", {
          modelId: chosen.modelId,
          voiceId: chosen.voiceId,
        }),
      );
    },

    async retention() {
      return settings(
        await request<Partial<RetentionSettings>>("/v1/settings/retention", "GET"),
      );
    },

    async setRetention(chosen) {
      // Both fields every time: the Engine takes the pair or rejects the
      // request (`422`), because a retention policy is the two knobs
      // together and a PATCH of one would leave the other ambiguous.
      const body = await patch<Partial<RetentionApplied>>("/v1/settings/retention", {
        segmentBudgetBytes: chosen.segmentBudgetBytes,
        keepAudioDays: chosen.keepAudioDays,
      });
      if (typeof body.evictedBytes !== "number") {
        throw new Error("The Engine sent retention settings this app cannot read.");
      }
      return { ...settings(body), evictedBytes: body.evictedBytes };
    },

    async openDataFolder() {
      await invoke("open_data_folder");
    },

    async revealLogs() {
      await invoke("reveal_logs");
    },

    async updateStatus() {
      return (await invoke("update_status")) as UpdateStatus;
    },

    async installUpdate() {
      await invoke("update_install");
    },

    downloadModel(modelId) {
      return post(`/v1/models/${encodeURIComponent(modelId)}/download`);
    },

    async deleteModel(modelId) {
      await request(`/v1/models/${encodeURIComponent(modelId)}`, "DELETE");
    },

    narrate(input, voice, mode) {
      // The chosen Voice travels with the request rather than being left
      // to the Engine's stored selection: the shell is the thing that
      // knows what the reader just picked, and the "try the fast voice"
      // offer narrates with a Voice that is deliberately not the stored one.
      // Speed stays the Engine's to resolve from its persisted playback
      // setting. Live changes use `/v1/settings/playback`, so copying that
      // value into a new speech request would create a second source of truth.
      return post(
        "/v1/audio/speech",
        voice
          ? { input, model: voice.modelId, voice: voice.voiceId, mode }
          : { input, mode },
      );
    },

    stop() {
      return post("/v1/audio/stop");
    },

    pause() {
      return post("/v1/audio/pause");
    },

    play() {
      return post("/v1/audio/play");
    },

    seek(sourceOffset) {
      return post("/v1/audio/seek", { sourceOffset });
    },

    seekTime(positionSec) {
      return post("/v1/audio/seek", { positionSec });
    },

    setSpeed(speed) {
      return patch("/v1/settings/playback", { speed });
    },

    async controls(voice) {
      return controlSettings(await request<unknown>(`/v1/settings/controls?${new URLSearchParams(voice)}`, "GET"));
    },
    async setControls(voice, overrides) {
      return controlSettings(await patch<unknown>("/v1/settings/controls", { ...voice, overrides }));
    },
    selectTake(narrationId, ordinal, action) {
      return post(`${route(narrationId)}/take`, { ordinal, action });
    },
    async listHistory() {
      const body = await request<{ history?: unknown }>("/v1/history", "GET");
      if (!Array.isArray(body.history)) {
        throw new Error("The Engine sent a History this app cannot read.");
      }
      return body.history as HistoryEntry[];
    },

    openNarration,

    resumeNarration(narrationId, mode, options) {
      const paused = options?.paused ? "&paused=true" : "";
      return post(`${route(narrationId)}/resume?mode=${mode}${paused}`);
    },

    async deleteNarration(narrationId) {
      const body = await request<Partial<Deletion>>(route(narrationId), "DELETE");
      const freed = body.audioBytesFreed ?? 0;
      return {
        // The Engine's own answer about what it deleted, rather than the id
        // we asked about echoed back. The reply's `deleted` is not carried:
        // a `200` is the deletion, and an id that was never there is a
        // `404` — a flag that is true whenever it exists says nothing.
        narrationId: body.narrationId ?? narrationId,
        // The freed disk is shown to a reader as a number they can check
        // against Settings, so a missing one reads as nothing freed rather
        // than as `NaN`.
        audioBytesFreed: Number.isFinite(freed) ? freed : 0,
      };
    },

    // Resolves `false` when the user dismissed the save panel, and `true`
    // once the Engine has *accepted* the Export — not once the file exists.
    // Writing it can mean re-synthesizing audio a retention sweep evicted,
    // which queues behind whatever is playing; progress arrives on
    // `/v1/export/events`.
    async exportNarration(narrationId, options = {}) {
      if (!save) throw new Error("The save panel is unavailable.");
      const format = options.format ?? "m4a";
      const destination = await save({
        defaultPath: options.defaultName
          ? `${options.defaultName}.${format}`
          : undefined,
        filters: [{ name: format.toUpperCase(), extensions: [format] }],
      });
      if (destination === null) return false;
      await post(`/v1/history/${encodeURIComponent(narrationId)}/export`, {
        destination,
        format,
      });
      return true;
    },
  };
};
