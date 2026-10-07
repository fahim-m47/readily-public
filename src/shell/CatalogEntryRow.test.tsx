import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { CatalogEntry, ModelStatus } from "../engine/client";
import CatalogEntryRow from "./CatalogEntryRow";

afterEach(cleanup);

const ENTRY: CatalogEntry = {
  id: "llama-tts:1b",
  name: "Llama TTS",
  tier: "expressive",
  supportModels: [],
  licenseTerms: {
    id: "llama3.1",
    name: "Llama 3.1 Community License",
    bindsReader: true,
    credit: "Built with Llama",
    attribution: null,
    text: "LLAMA 3.1 COMMUNITY LICENSE AGREEMENT",
  },
  ramClassGb: 3,
  voices: [{ simple: true, id: "voice", name: "Voice", language: "en-US", preview: null }],
  defaultVoiceId: "voice",
  downloadBytes: 1024 * 1024 * 1700,
  runsHere: true,
};

const STATUS: ModelStatus = {
  id: ENTRY.id,
  installed: false,
  diskBytes: 0,
  downloadBytes: ENTRY.downloadBytes,
};

test("a reader-binding entry shows its credit and acceptance link", () => {
  render(
    <ul>
      <CatalogEntryRow
        entry={ENTRY}
        status={STATUS}
        download={null}
        audition={{ playing: null, failed: null, toggle: vi.fn(), stop: vi.fn(), level: () => 0 }}
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onWithdraw={vi.fn()}
      />
    </ul>,
  );

  const row = screen.getByText("Llama TTS").closest<HTMLElement>(".model");
  if (row === null) throw new Error("the Catalog entry row did not render");
  expect(within(row).getByText("Built with Llama")).toBeTruthy();
  expect(
    within(row).getByRole("button", { name: "Llama 3.1 Community License" }),
  ).toBeTruthy();
});

test("the shared alignment licence is readable without becoming a Voice choice", () => {
  render(
    <ul>
      <CatalogEntryRow
        entry={{
          ...ENTRY,
          supportModels: [{
            name: "Word alignment",
            licenseTerms: {
              id: "Apache-2.0", name: "Apache 2.0", bindsReader: false,
              credit: null, attribution: null, text: "Shared aligner licence text",
            },
          }],
        }}
        status={STATUS}
        download={null}
        audition={{ playing: null, failed: null, toggle: vi.fn(), stop: vi.fn(), level: () => 0 }}
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onWithdraw={vi.fn()}
      />
    </ul>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Word alignment licence" }));
  expect(screen.getByText("Shared aligner licence text")).toBeTruthy();
  expect(screen.getAllByRole("button", { name: /^Download/ })).toHaveLength(1);
});

test("an entry this machine cannot run says why instead of offering a download", () => {
  const onDownload = vi.fn();
  render(
    <ul>
      <CatalogEntryRow
        entry={{ ...ENTRY, runsHere: false }}
        status={STATUS}
        download={null}
        audition={{ playing: null, failed: null, toggle: vi.fn(), stop: vi.fn(), level: () => 0 }}
        onDownload={onDownload}
        onDelete={vi.fn()}
        onWithdraw={vi.fn()}
      />
    </ul>,
  );

  const row = screen.getByText("Llama TTS").closest<HTMLElement>(".model");
  if (row === null) throw new Error("the Catalog entry row did not render");
  expect(row.classList).toContain("model--unrunnable");
  expect(within(row).getByText("Needs a Mac with Apple silicon")).toBeTruthy();
  expect(within(row).queryByRole("button", { name: /Download/ })).toBeNull();
  // Its Voices stay auditionable: the previews are app resources, not the model.
  expect(within(row).getByRole("button", { name: "Hear Voice" })).toBeTruthy();
});

test("an entry this machine cannot run stays unrunnable once on disk, but can be deleted", () => {
  render(
    <ul>
      <CatalogEntryRow
        entry={{ ...ENTRY, runsHere: false }}
        status={{ ...STATUS, installed: true, diskBytes: ENTRY.downloadBytes }}
        download={null}
        audition={{ playing: null, failed: null, toggle: vi.fn(), stop: vi.fn(), level: () => 0 }}
        onDownload={vi.fn()}
        onDelete={vi.fn()}
        onWithdraw={vi.fn()}
      />
    </ul>,
  );

  const row = screen.getByText("Llama TTS").closest<HTMLElement>(".model");
  if (row === null) throw new Error("the Catalog entry row did not render");
  expect(row.classList).toContain("model--unrunnable");
  expect(within(row).getByRole("button", { name: /Delete/ })).toBeTruthy();
});
