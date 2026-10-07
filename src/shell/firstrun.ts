// The app's first minutes, in plain language.
//
// The `.app` ships no Python and no Voice Model, so a clean machine has two
// long waits ahead of it before anything can be read aloud: the Engine's
// environment is built from the bundled `uv` (ADR 0001 §6), and then the
// default Voice Model has to land. Both are multi-minute and neither can be
// done offline.
//
// This file decides which beat the reader is on and what it says. Every beat
// covers the app until the first time it is usable; after that the shell
// never gives the screen back (`useFirstRun`). So an ordinary launch waits
// here for the Engine too, rather than showing a composer that cannot narrate
// with the reason in the sidebar's corner.

import type { CatalogEntry, Connection, DownloadState } from "../engine/client";
import { describeDownloadRetry } from "./catalog";
import { formatBytes } from "./estimate";
import type { Licence } from "./terms";

// The beats of a first run. `starting`, `waiting` and `terms` are the ones
// an ordinary launch can pass through too.
export type FirstRunKind =
  | "provisioning"
  | "engine-failed"
  | "store-failed"
  | "getting-voice-model"
  | "downloading"
  | "verifying"
  | "download-failed"
  | "starting"
  | "waiting"
  | "terms";

export type FirstRunStep = {
  kind: FirstRunKind;
  // `asking` is the licence beat: nothing is happening until the reader
  // answers, so it has no spinner.
  tone: "working" | "failed" | "asking";
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
  // The Catalog's licences the reader has not accepted yet, or `null` before
  // the Catalog has arrived.
  unaccepted: Licence[] | null;
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
  unaccepted,
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

  const storeFailed = () =>
    failed(
      "store-failed",
      "Readily could not check what is already downloaded.",
      "Nothing was lost. Try again.",
      "store",
      storeFailure,
    );

  // Before anything is downloaded, including the first run's own fetch: the
  // terms are the Catalog's, so nothing can be asked until it is read. A
  // Catalog that could not be read offers nothing to download either, so it
  // does not stand between a reader and the models already on disk.
  if (unaccepted === null && storeFailure === null) return working("waiting", "Nearly ready…");
  if (unaccepted !== null && unaccepted.length > 0) {
    return {
      kind: "terms",
      tone: "asking",
      message: "Readily exclusively supports free open-source models",
      detail: null,
      aside: null,
      progress: null,
      retry: null,
    };
  }

  if (anyVoiceModel === true) return null;
  if (storeFailure !== null) return storeFailed();
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
