import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { CatalogLicense, ReferenceLicense } from "../engine/client";
import LicenceView from "./LicenceView";

const openUrl = vi.hoisted(() => vi.fn<(url: string) => Promise<void>>());
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl }));

afterEach(cleanup);
afterEach(() => openUrl.mockReset());

const SOURCE = "https://huggingface.co/acme/chatter/tree/abc";

const ATTRIBUTION: NonNullable<CatalogLicense["attribution"]> = {
  creator: "Acme Audio",
  copyrightNotice: "Copyright 2026 Acme Audio\nCopyright 2025 Contributors",
  source: SOURCE,
  warrantyNotice:
    "Section 5 – Disclaimer of Warranties and Limitation of Liability.",
  modified: true,
};

const CC_BY: CatalogLicense = {
  id: "CC-BY-4.0",
  name: "Creative Commons Attribution 4.0",
  bindsReader: false,
  credit: null,
  attribution: ATTRIBUTION,
  text: "Creative Commons Attribution 4.0 International Public License",
};

const APACHE: CatalogLicense = {
  id: "Apache-2.0",
  name: "Apache 2.0",
  bindsReader: false,
  credit: null,
  attribution: null,
  text: "<img src=x onerror=alert(1)>Terms & conditions",
};

const CLIP_SOURCE = "https://datashare.ed.ac.uk/handle/10283/3443";

const VCTK: ReferenceLicense = {
  id: "CC-BY-4.0",
  name: "Creative Commons Attribution 4.0",
  text: "Creative Commons Attribution 4.0 International Public License",
  warrantyNotice:
    "Section 5 – Disclaimer of Warranties and Limitation of Liability.",
  clips: [
    {
      voice: "Avery",
      creator: "CSTR, University of Edinburgh",
      copyrightNotice: "Copyright 2019 University of Edinburgh",
      source: CLIP_SOURCE,
      modified: true,
    },
    {
      voice: "Mason",
      creator: "CSTR, University of Edinburgh",
      copyrightNotice: "Copyright 2019 University of Edinburgh",
      source: CLIP_SOURCE,
      modified: false,
    },
  ],
};

const renderLicence = (
  terms: CatalogLicense = CC_BY,
  referenceLicenses?: ReferenceLicense[],
) => {
  render(
    <LicenceView
      entryName="Chatter"
      terms={terms}
      referenceLicenses={referenceLicenses}
      onClose={vi.fn()}
    />,
  );
  return screen.getByRole("dialog", { name: terms.name });
};

test("the licence view carries the attribution the wire holds", () => {
  const licence = renderLicence();

  expect(within(licence).getByText("Acme Audio")).toBeTruthy();
  expect(
    within(licence).getByText(
      "Copyright 2026 Acme Audio Copyright 2025 Contributors",
    ),
  ).toBeTruthy();
  expect(within(licence).getByText(SOURCE)).toBeTruthy();
  expect(
    within(licence).getByText(
      "Section 5 – Disclaimer of Warranties and Limitation of Liability.",
    ),
  ).toBeTruthy();
  expect(within(licence).getByText("Modified by Readily.")).toBeTruthy();
});

test("a licence without attribution renders its text as inert text", () => {
  const licence = renderLicence(APACHE);

  expect(licence.querySelector(".licence__facts")).toBe(null);
  expect(within(licence).getByText(APACHE.text)).toBeTruthy();
  expect(licence.querySelector("img")).toBe(null);
});

test("an unmodified entry's licence view claims no modification", () => {
  const terms: CatalogLicense = {
    ...CC_BY,
    attribution: { ...ATTRIBUTION, modified: false },
  };

  const licence = renderLicence(terms);

  expect(within(licence).queryByText("Modified by Readily.")).toBe(null);
});

test("the source opens in the reader's browser", async () => {
  openUrl.mockResolvedValue(undefined);
  const licence = renderLicence();
  const source = within(licence).getByRole("link", { name: SOURCE });

  expect(source.getAttribute("href")).toBe(SOURCE);
  expect(fireEvent.click(source)).toBe(false);
  await waitFor(() => expect(openUrl).toHaveBeenCalledWith(SOURCE));
});

test("a source the host will not open leaves the address", async () => {
  openUrl.mockRejectedValue(new Error("no opener here"));
  const licence = renderLicence();

  fireEvent.click(within(licence).getByRole("link", { name: SOURCE }));

  expect(
    await within(licence).findByText(/the address can be copied/),
  ).toBeTruthy();
  expect(within(licence).getByText(SOURCE)).toBeTruthy();
});

test("the clips an entry's Voices are cut from are credited under the weights' terms", () => {
  const licence = renderLicence(APACHE, [VCTK]);

  const weights = within(licence).getByText(APACHE.text);
  const clips = within(licence).getByRole("region", {
    name: "Voice clips under Creative Commons Attribution 4.0",
  });
  // The weights' terms come first; the clips' credit follows them.
  expect(
    weights.compareDocumentPosition(clips) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(within(clips).getByText("Avery")).toBeTruthy();
  expect(within(clips).getByText("Mason")).toBeTruthy();
  expect(within(clips).getAllByText("CSTR, University of Edinburgh")).toHaveLength(2);
  expect(within(clips).getAllByRole("link", { name: CLIP_SOURCE })).toHaveLength(2);
  expect(within(clips).getAllByText("Modified by Readily.")).toHaveLength(1);
  expect(within(clips).getByText(VCTK.warrantyNotice)).toBeTruthy();
  expect(within(clips).getByText(VCTK.text)).toBeTruthy();
});

test("an entry whose clips are its own credits no corpus", () => {
  const licence = renderLicence(APACHE, []);

  expect(within(licence).queryByRole("region")).toBe(null);
});
