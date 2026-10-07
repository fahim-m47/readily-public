import { strToU8, zipSync } from "fflate";
import { expect, test, vi } from "vitest";
import { readDocx } from "./docx";

// jsdom has no Worker, so the unzip runs in-thread.
vi.mock("./unzip", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./unzip")>();
  return { ...actual, unzip: async (bytes: Uint8Array, want: import("./unzip").Want) => actual.unzipEntries(bytes, want) };
});

const W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";

const docx = (body: string, parts: Record<string, string> = {}) =>
  new File(
    [
      zipSync({
        "word/document.xml": strToU8(
          `<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="${W}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><w:body>${body}</w:body></w:document>`,
        ),
        ...Object.fromEntries(Object.entries(parts).map(([name, xml]) => [name, strToU8(xml)])),
      }),
    ],
    "report.docx",
  );

test("reads paragraphs as paragraphs, with tabs and line breaks inside them", async () => {
  const file = docx(
    `<w:p><w:r><w:t>First</w:t></w:r><w:r><w:t xml:space="preserve"> paragraph.</w:t></w:r></w:p>` +
      `<w:p><w:r><w:t>Name</w:t><w:tab/><w:t>Value</w:t><w:br/><w:t>next line</w:t></w:r></w:p>`,
  );

  expect(await readDocx(file)).toEqual({ ok: true, text: "First paragraph.\n\nName\tValue\nnext line" });
});

test("keeps tracked insertions and drops tracked deletions", async () => {
  const file = docx(
    `<w:p><w:r><w:t xml:space="preserve">The </w:t></w:r>` +
      `<w:del><w:r><w:delText>old</w:delText></w:r></w:del>` +
      `<w:ins><w:r><w:t>new</w:t></w:r></w:ins>` +
      `<w:r><w:t xml:space="preserve"> wording.</w:t></w:r></w:p>`,
  );

  expect(await readDocx(file)).toEqual({ ok: true, text: "The new wording." });
});

test("reads a text box once, not again from its fallback copy", async () => {
  const file = docx(
    `<w:p><w:r><mc:AlternateContent>` +
      `<mc:Choice Requires="wps"><w:p><w:r><w:t>In a box.</w:t></w:r></w:p></mc:Choice>` +
      `<mc:Fallback><w:p><w:r><w:t>In a box.</w:t></w:r></w:p></mc:Fallback>` +
      `</mc:AlternateContent></w:r></w:p>`,
  );

  expect(await readDocx(file)).toEqual({ ok: true, text: "In a box." });
});

test("reads table cells and skips headers and footers", async () => {
  const file = docx(
    `<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell one</w:t></w:r></w:p></w:tc>` +
      `<w:tc><w:p><w:r><w:t>Cell two</w:t></w:r></w:p></w:tc></w:tr></w:tbl>`,
    { "word/header1.xml": `<w:hdr xmlns:w="${W}"><w:p><w:r><w:t>Page header</w:t></w:r></w:p></w:hdr>` },
  );

  expect(await readDocx(file)).toEqual({ ok: true, text: "Cell one\n\nCell two" });
});

test("reads a document saved as UTF-16, as Word and macOS do", async () => {
  const xml = `\ufeff<?xml version="1.0" encoding="UTF-16"?><w:document xmlns:w="${W}"><w:body><w:p><w:r><w:t>Hello from UTF-16.</w:t></w:r></w:p></w:body></w:document>`;
  const view = new DataView(new ArrayBuffer(xml.length * 2));
  [...xml].forEach((unit, i) => view.setUint16(2 * i, unit.charCodeAt(0), true));
  const file = new File([zipSync({ "word/document.xml": new Uint8Array(view.buffer) })], "utf16.docx");

  expect(await readDocx(file)).toEqual({ ok: true, text: "Hello from UTF-16." });
});

test("calls a zip with no Word document in it damaged", async () => {
  const file = new File([zipSync({ "mimetype": strToU8("application/epub+zip") })], "fake.docx");

  expect(await readDocx(file)).toEqual({ ok: false, why: "damaged" });
});
