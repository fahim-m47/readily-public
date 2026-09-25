import { expect, test } from "vitest";
import type { CatalogEntry, CatalogVoice, DownloadState } from "../engine/client";
import {
  describeDetail,
  describeLicence,
  describeModelDeletion,
  describeRowDownload,
  describeVoices,
  previewUrl,
} from "./catalog";

const VOICE: CatalogVoice = {
  simple: true,
  id: "af_heart",
  name: "Heart",
  language: "en-US",
  preview: "kokoro/82m/af_heart.m4a",
};

const ENTRY: CatalogEntry = {
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
  voices: [VOICE],
  defaultVoiceId: "af_heart",
  downloadBytes: 1024 * 1024 * 337,
};

const DOWNLOAD: DownloadState = {
  version: 1,
  phase: "downloading",
  modelId: "kokoro:82m",
  bytesTotal: 1024 * 1024 * 337,
  bytesDownloaded: 1024 * 1024 * 120,
  error: null,
};

test("a preview clip resolves inside the app's own bundle", () => {
  expect(previewUrl(VOICE)).toBe("/previews/kokoro/82m/af_heart.m4a");
});

test("a Voice with no clip yet has nothing to play", () => {
  expect(previewUrl({ ...VOICE, preview: null })).toBeNull();
});

test.each([
  ["../../etc/passwd.m4a", "climbs out of the preview root"],
  ["//evil.example/clip.m4a", "would become protocol-relative"],
  ["https://evil.example/clip.m4a", "is not a path at all"],
  ["kokoro/82m/af_heart.m4a?x=1", "smuggles a query onto the path"],
  ["kokoro/82m/af_heart.wav", "is not the one container"],
])("a preview path that %s plays nothing", (preview) => {
  expect(previewUrl({ ...VOICE, preview })).toBeNull();
});

test("Voices are counted before anything is downloaded", () => {
  expect(describeVoices([VOICE])).toBe("1 voice");
  expect(describeVoices([VOICE, { ...VOICE, id: "am_puck" }])).toBe("2 voices");
  expect(
    describeVoices([VOICE, { ...VOICE, id: "bf_emma", language: "en-GB" }]),
  ).toBe("2 voices");
  expect(
    describeVoices([VOICE, { ...VOICE, id: "ff_siwis", language: "fr-FR" }]),
  ).toBe("2 voices · 2 languages");
});

test("the detail row is download size, memory class and licence", () => {
  expect(describeDetail(ENTRY, undefined)).toEqual({
    cost: "337.0 MB download · 0.5 GB memory",
    licence: "Apache 2.0",
  });
});

test("an installed entry reports the disk it actually takes, not the fetch", () => {
  const status = {
    id: ENTRY.id,
    installed: true,
    diskBytes: 1024 * 1024 * 340,
    downloadBytes: ENTRY.downloadBytes,
  };

  expect(describeDetail(ENTRY, status)).toEqual({
    cost: "340.0 MB on disk · 0.5 GB memory",
    licence: "Apache 2.0",
  });
});

test("a memory class never claims a precision it does not have", () => {
  expect(describeDetail({ ...ENTRY, ramClassGb: 3 }, undefined).cost).toContain(
    "3 GB memory",
  );
});

test("a licence that binds the reader says the download is the acceptance", () => {
  const terms = {
    ...ENTRY.licenseTerms,
    id: "llama3.1",
    name: "Llama 3.1 Community License",
    bindsReader: true,
  };

  expect(describeLicence(terms)).toBe(
    "downloading accepts the Llama 3.1 Community License",
  );
  expect(describeDetail({ ...ENTRY, licenseTerms: terms }, undefined)).toEqual({
    cost: "337.0 MB download · 0.5 GB memory",
    licence: "downloading accepts the Llama 3.1 Community License",
  });
});

test("a licence that binds nobody but Readily is named and nothing more", () => {
  expect(describeLicence(ENTRY.licenseTerms)).toBe("Apache 2.0");
});

test("a download in flight says how far along it is", () => {
  const row = describeRowDownload("kokoro:82m", DOWNLOAD);

  expect(row.line?.message).toBe("Downloading — 120.0 MB of 337.0 MB");
  expect(row.line?.progress).toEqual({
    done: DOWNLOAD.bytesDownloaded,
    total: DOWNLOAD.bytesTotal,
  });
  expect(row.inFlight).toBe(true);
  expect(row.busyElsewhere).toBe(false);
});

test("one download at a time, so every other entry is told to wait", () => {
  const row = describeRowDownload("qwen3-tts:0.6b", DOWNLOAD);

  expect(row.line).toBeNull();
  expect(row.inFlight).toBe(false);
  expect(row.busyElsewhere).toBe(true);
});

test("verification gets its own beat, and no bar to sit at the end of", () => {
  const { line } = describeRowDownload("kokoro:82m", {
    ...DOWNLOAD,
    phase: "verifying",
  });

  expect(line?.message).toBe("Checking the files match what Readily expects…");
  expect(line?.progress).toBeNull();
});

test("a download that failed on the network says trying again resumes", () => {
  const { line, inFlight } = describeRowDownload("kokoro:82m", {
    ...DOWNLOAD,
    phase: "failed",
    error: {
      version: 1,
      code: "download_failed",
      message: "The download could not be completed.",
    },
  });

  expect(line?.tone).toBe("failed");
  expect(line?.message).toBe(
    "The download could not be completed. Trying again picks up where it stopped.",
  );
  expect(inFlight).toBe(false);
});

test("a download that failed verification does not promise a resume", () => {
  // The Engine discards staged bytes that missed their pinned hashes, so a
  // retry starts clean (`docs/wire.md`).
  const { line } = describeRowDownload("kokoro:82m", {
    ...DOWNLOAD,
    phase: "failed",
    error: {
      version: 1,
      code: "verification_failed",
      message: "The downloaded files did not match the Catalog.",
    },
  });

  expect(line?.message).toBe(
    "The downloaded files did not match the Catalog. Trying again downloads it fresh.",
  );
});

test("a folder the store cannot write says the retry waits on the folder", () => {
  // Nothing was fetched and nothing is staged: the Engine refused up
  // front, and the message it sent names the folder.
  const { line } = describeRowDownload("kokoro:82m", {
    ...DOWNLOAD,
    phase: "failed",
    error: {
      version: 1,
      code: "store_unwritable",
      message: "The folder /models/kokoro is not writable.",
    },
  });

  expect(line?.message).toBe(
    "The folder /models/kokoro is not writable. Trying again works once that folder can be written.",
  );
});

test("a download about another entry is not this entry's line", () => {
  expect(describeRowDownload("kokoro:82m", null).line).toBeNull();
  const done = describeRowDownload("kokoro:82m", {
    ...DOWNLOAD,
    phase: "installed",
  });
  expect(done.line).toBeNull();
  expect(done.inFlight).toBe(false);
  expect(done.busyElsewhere).toBe(false);
});

test("deleting a Voice Model says the disk it freed", () => {
  expect(describeModelDeletion("Kokoro", 1024 * 1024 * 337)).toBe(
    "Kokoro deleted. 337.0 MB freed.",
  );
  expect(describeModelDeletion("Kokoro", 0)).toBe("Kokoro deleted.");
});
