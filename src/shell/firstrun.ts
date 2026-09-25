// The app's first minutes, in plain language.
//
// The `.app` ships no Python and no Voice Model, so a clean machine has two
// long waits ahead of it before anything can be read aloud: the Engine's
// environment is built from the bundled `uv` (ADR 0001 §6), and then the
// default Voice Model has to land. Both are multi-minute and neither can be
// done offline.
//
// This file decides which beat the reader is on and what it says. Whether
// that beat takes the screen over is `useFirstRun`'s question.

import type { CatalogEntry, Connection, DownloadState } from "../engine/client";
import { describeDownloadRetry } from "./catalog";
import { formatBytes } from "./estimate";

// The beats of a first run. `starting` and `waiting` are the states an
// ordinary launch also passes through, so they are named but never grounds
// to cover the app up; the app cannot be used through any other beat.
export type FirstRunKind =
  | "provisioning"
  | "engine-failed"
  | "store-failed"
  | "getting-voice-model"
  | "downloading"
  | "verifying"
  | "download-failed"
  | "starting"
  | "waiting";

// Whether this beat is grounds to cover the app up.
//
// Not "is this a first run": a dead Engine and a store that refused are both
// here, and either can happen to a long-provisioned machine. What they share
// with the download beats is that there is no Voice Model to narrate with and
// no way to find out whether there is one, so a composer underneath would
// only offer a button that answers `409`.
export const coversTheApp = (kind: FirstRunKind) =>
  kind !== "starting" && kind !== "waiting";

export type FirstRunStep = {
  kind: FirstRunKind;
  tone: "working" | "failed";
  // The headline: what is happening, said the way a person would say it.
  message: string;
  // The line under it: why the wait is as long as it is, or what pressing
  // the button will actually do. `null` when the headline is the whole
  // story.
  detail: string | null;
  // The machine's own words, kept under both sentences above rather than in
  // place of them: `uv`'s latest line while the environment builds, and the
  // supervisor's raw reason once it has given up.
  aside: string | null;
  // Bytes so far against bytes in total, or `null` when there is no honest
  // bar to draw — the same rule the Catalog sheet follows.
  progress: { done: number; total: number } | null;
  // Which retry to offer, or `null` when nothing is stuck. The three things
  // that can fail on a first run are asked for in three different places —
  // the supervisor's launch, the reads of what is on disk, and the Engine's
  // download.
  retry: "engine" | "store" | "download" | null;
};

const working = (
  kind: FirstRunKind,
  message: string,
  detail: string | null = null,
  extra: Partial<Pick<FirstRunStep, "aside" | "progress">> = {},
): FirstRunStep => ({
  kind,
  tone: "working",
  message,
  detail,
  aside: null,
  progress: null,
  retry: null,
  ...extra,
});

const failed = (
  kind: FirstRunKind,
  message: string,
  detail: string,
  retry: FirstRunStep["retry"],
  aside: string | null = null,
): FirstRunStep => ({
  kind,
  tone: "failed",
  message,
  detail,
  aside,
  progress: null,
  retry,
});

// Everything the screen's next beat depends on, gathered rather than passed
// one by one: any one of them can be the reason a first run is stuck.
export type FirstRunFacts = {
  connection: Connection;
  // Whether there is *any* Voice Model on disk — the weak question on
  // purpose, since this screen exists to get the machine to one rather than
  // to honour a stored choice. `null` is the store not having answered,
  // which is not an empty disk.
  anyVoiceModel: boolean | null;
  // The entry the first Voice Model will be, or `null` before the Catalog
  // has arrived.
  firstVoiceModel: CatalogEntry | null;
  // Why the Catalog or the model store could not be read, if either refused.
  storeFailure: string | null;
  // The Engine's snapshot of the one download it runs.
  download: DownloadState | null;
  // Why the download could not be started, or why its progress stopped
  // arriving.
  downloadFailure: string | null;
};

// Where the app is on its way to being usable, or `null` once it is.
export const firstRunStep = ({
  connection,
  anyVoiceModel,
  firstVoiceModel,
  storeFailure,
  download,
  downloadFailure,
}: FirstRunFacts): FirstRunStep | null => {
  if (connection.state === "failed") {
    return failed(
      "engine-failed",
      "Readily could not finish setting itself up.",
      "Setting up needs the network for this first run. Check the connection, then try again.",
      "engine",
      connection.message,
    );
  }

  if (connection.state === "starting") {
    if (connection.detail === "provisioning") {
      return working(
        "provisioning",
        "Setting up Readily…",
        "This happens once. Readily is fetching the pieces it needs to read out loud, which can take a few minutes.",
        // No bar: `uv` resolves the wheels as it goes, so there is no total
        // to count against.
        { aside: connection.note },
      );
    }
    return working(
      "starting",
      connection.detail === "restarting"
        ? "Restarting the Engine…"
        : "Starting the Engine…",
    );
  }

  if (anyVoiceModel === true) return null;
  if (storeFailure !== null) {
    return failed(
      "store-failed",
      "Readily could not check what is already downloaded.",
      "Nothing was lost. Try again.",
      "store",
      storeFailure,
    );
  }
  if (anyVoiceModel === null || firstVoiceModel === null) {
    return working("waiting", "Nearly ready…");
  }

  const mine = download?.modelId === firstVoiceModel.id ? download : null;
  switch (mine?.phase) {
    case "downloading":
      return working(
        "downloading",
        "Getting the first voice model…",
        `${firstVoiceModel.name} — ${formatBytes(mine.bytesDownloaded)} of ${formatBytes(mine.bytesTotal)}`,
        { progress: { done: mine.bytesDownloaded, total: mine.bytesTotal } },
      );
    case "verifying":
      return working(
        "verifying",
        "Getting the first voice model…",
        "Checking the files match what Readily expects…",
      );
    case "failed":
      return failed(
        "download-failed",
        mine.error?.message ?? "The download did not finish.",
        describeDownloadRetry(mine.error),
        "download",
      );
    case "installed":
      // The bytes are down but the store has not been re-read yet, so the
      // app still cannot narrate.
      return working("waiting", "Nearly ready…");
    default:
      return downloadFailure !== null
        ? failed(
            "download-failed",
            downloadFailure,
            "Trying again picks up where it stopped.",
            "download",
          )
        : working(
            "getting-voice-model",
            "Getting the first voice model…",
            `${firstVoiceModel.name} · ${formatBytes(firstVoiceModel.downloadBytes)} to download`,
          );
  }
};
