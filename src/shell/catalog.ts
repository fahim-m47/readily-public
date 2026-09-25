// What the Catalog sheet says about a Voice Model, in plain language.

import type {
  CatalogEntry,
  CatalogLicense,
  CatalogVoice,
  DownloadState,
  ModelStatus,
  WireError,
} from "../engine/client";
import { formatBytes } from "./estimate";

// Where the app keeps its bundled Voice Preview clips, as a URL path. They
// are app resources served from the webview's own origin — auditioning a
// Voice is a bundle read, never an Engine call and never a network one.
const PREVIEW_ROOT = "previews/";

// A path segment the manifest's own validator would have accepted. Checked
// again here because this one becomes a URL: the Engine is trusted, but a
// path that turned into `//host/clip.m4a` would be the UI fetching from
// the network, and the privacy invariant is not a thing to hold on trust.
const SAFE_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

// The URL for a Voice's bundled preview clip, or `null` when there is no
// clip to play — either because curation has not made one, or because the
// path is not one this app will load.
export const previewUrl = (voice: CatalogVoice): string | null => {
  const path = voice.preview;
  if (path === null || !path.endsWith(".m4a")) return null;
  if (!path.split("/").every((segment) => SAFE_SEGMENT.test(segment))) return null;

  // Not redundant with `SAFE_SEGMENT`: `BASE_URL` is a build setting, and a
  // `base` pointing at a CDN turns every clip into a network fetch without a
  // single segment changing. Checked on the resolved URL, not the parts.
  const candidate = `${import.meta.env.BASE_URL}${PREVIEW_ROOT}${path}`;
  const resolved = new URL(candidate, window.location.href);
  if (resolved.origin !== window.location.origin) return null;
  return candidate;
};

// Memory as a class rather than a measurement: `0.5` reads "0.5 GB", `3`
// reads "3 GB" — a trailing `.0` would claim precision the number lacks.
const describeMemory = (ramClassGb: number) =>
  `${Number(ramClassGb.toFixed(1))} GB`;

// The entry's Voices, counted before anything is downloaded. Languages are
// counted by base tag, so en-US and en-GB read as one language.
export const describeVoices = (voices: readonly CatalogVoice[]) => {
  const languages = new Set(
    voices.map((voice) => voice.language.split("-")[0]),
  ).size;
  const counted = `${voices.length} ${voices.length === 1 ? "voice" : "voices"}`;
  return languages > 1 ? `${counted} · ${languages} languages` : counted;
};

export const describeLicence = (terms: CatalogLicense) =>
  terms.bindsReader ? `downloading accepts the ${terms.name}` : terms.name;

export type DetailLine = { cost: string; licence: string };

export const describeDetail = (
  entry: CatalogEntry,
  status?: ModelStatus,
): DetailLine => {
  const disk =
    status?.installed && status.diskBytes > 0
      ? `${formatBytes(status.diskBytes)} on disk`
      : `${formatBytes(entry.downloadBytes)} download`;
  return {
    cost: `${disk} · ${describeMemory(entry.ramClassGb)} memory`,
    licence: describeLicence(entry.licenseTerms),
  };
};

// What pressing "Try again" on a failed download will actually do. A network
// failure resumes from the bytes already staged; bytes that failed their
// pinned hashes were discarded; a folder the store cannot write refuses
// until the reader fixes it (`docs/wire.md`, ADR 0003 §3).
export const describeDownloadRetry = (error: WireError | null) => {
  switch (error?.code) {
    case "verification_failed":
      return "Trying again downloads it fresh.";
    case "store_unwritable":
      return "Trying again works once that folder can be written.";
    default:
      return "Trying again picks up where it stopped.";
  }
};

export type DownloadLine = {
  tone: "working" | "failed";
  message: string;
  // Bytes so far and bytes in total, for the progress bar, or `null` when
  // there is no measurable progress to draw — verification has no bytes of
  // its own.
  progress: { done: number; total: number } | null;
};

// What the sheet says under the entry the Engine is working on, or `null`
// when this entry is not the one. `verifying` gets its own beat: it is the
// moment the store decides whether what arrived is what the Manifest pinned
// (ADR 0003 §3).
const describeDownload = (
  entryId: string,
  download: DownloadState | null,
): DownloadLine | null => {
  if (!download || download.modelId !== entryId) return null;
  const progress = { done: download.bytesDownloaded, total: download.bytesTotal };

  switch (download.phase) {
    case "downloading":
      return {
        tone: "working",
        message: `Downloading — ${formatBytes(download.bytesDownloaded)} of ${formatBytes(download.bytesTotal)}`,
        progress,
      };
    case "verifying":
      return {
        tone: "working",
        message: "Checking the files match what Readily expects…",
        progress: null,
      };
    case "failed":
      return {
        tone: "failed",
        message: `${download.error?.message ?? "The download did not finish."} ${describeDownloadRetry(download.error)}`,
        progress: null,
      };
    default:
      return null;
  }
};

// "Deleting visibly frees disk — the delete button is not a lie" (ADR 0004
// §6).
export const describeModelDeletion = (name: string, diskBytes: number) =>
  diskBytes > 0
    ? `${name} deleted. ${formatBytes(diskBytes)} freed.`
    : `${name} deleted.`;

export type RowDownload = {
  // What to say under this entry, or `null` when the Engine is not working
  // on it.
  line: DownloadLine | null;
  // This entry's own download is running. Not the same as having a line: a
  // failed download still has one.
  inFlight: boolean;
  // Another entry is downloading. One at a time is the Engine's rule
  // (`docs/wire.md`), so a button here would come back `409`.
  busyElsewhere: boolean;
};

// Everything one row needs to know about the Engine's single download, in
// one derivation.
export const describeRowDownload = (
  entryId: string,
  download: DownloadState | null,
): RowDownload => {
  const phase = download?.phase;
  const running = phase === "downloading" || phase === "verifying";
  const mine = download?.modelId === entryId;
  return {
    line: describeDownload(entryId, download),
    inFlight: running && mine,
    busyElsewhere: running && !mine,
  };
};
