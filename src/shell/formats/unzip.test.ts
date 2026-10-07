import { strToU8, zipSync } from "fflate";
import { expect, test } from "vitest";
import { INFLATE_CAP, unzipEntries } from "./unzip";

test("inflates only the entries asked for", () => {
  const zip = zipSync({
    "word/document.xml": strToU8("<w:document/>"),
    "word/header1.xml": strToU8("<w:hdr/>"),
    "stored.txt": [strToU8("kept as is"), { level: 0 }],
  });

  const result = unzipEntries(zip, { names: ["word/document.xml", "stored.txt"] });

  expect(result).toEqual({
    ok: true,
    files: new Map([
      ["word/document.xml", "<w:document/>"],
      ["stored.txt", "kept as is"],
    ]),
    inflated: 23,
  });
});

test("refuses an archive that inflates past the cap, whatever its headers claim", () => {
  // A zip bomb: a few kilobytes that inflate to more than the cap.
  const zip = zipSync({ "word/document.xml": new Uint8Array(INFLATE_CAP + 1) });
  expect(zip.length).toBeLessThan(64 * 1024);

  expect(unzipEntries(zip, { names: ["word/document.xml"] })).toEqual({ ok: false, why: "too-big" });
});

test("counts the cap across every entry, not per entry", () => {
  const half = new Uint8Array(INFLATE_CAP / 2 + 1);
  const zip = zipSync({ "a.xhtml": half, "b.xhtml": half });

  expect(unzipEntries(zip, { names: ["a.xhtml", "b.xhtml"] })).toEqual({ ok: false, why: "too-big" });
});

test("counts what earlier passes over the archive inflated against the same cap", () => {
  const half = new Uint8Array(INFLATE_CAP / 2 + 1);
  const zip = zipSync({ "a.xhtml": half, "b.xhtml": half });

  const first = unzipEntries(zip, { names: ["a.xhtml"] });
  expect(first.ok && first.inflated).toBe(half.length);
  expect(unzipEntries(zip, { names: ["b.xhtml"], inflated: half.length })).toEqual({ ok: false, why: "too-big" });
});

test("decodes an entry by its byte-order mark, UTF-16 either way round", () => {
  const zip = zipSync({
    "le.xml": new Uint8Array([0xff, 0xfe, 0x3c, 0x00, 0xe9, 0x00, 0x2f, 0x00, 0x3e, 0x00]),
    "be.xml": new Uint8Array([0xfe, 0xff, 0x00, 0x3c, 0x00, 0xe9, 0x00, 0x2f, 0x00, 0x3e]),
    "utf8.xml": new Uint8Array([0xef, 0xbb, 0xbf, 0x3c, 0xc3, 0xa9, 0x2f, 0x3e]),
  });

  const result = unzipEntries(zip, { names: ["le.xml", "be.xml", "utf8.xml"] });

  expect(result.ok && [...result.files.values()]).toEqual(["<é/>", "<é/>", "<é/>"]);
});

// The text as UTF-16 code units in the given byte order, with or without a
// byte order mark in front.
const utf16 = (text: string, endian: "le" | "be", bom = true) => {
  const units = bom ? `\ufeff${text}` : text;
  const view = new DataView(new ArrayBuffer(units.length * 2));
  [...units].forEach((unit, i) => view.setUint16(2 * i, unit.charCodeAt(0), endian === "le"));
  return new Uint8Array(view.buffer);
};

test("decodes each entry the way its XML declares, not always as UTF-8", () => {
  const declared = (encoding: string) => `<?xml version="1.0" encoding="${encoding}"?><w:t>café</w:t>`;
  const zip = zipSync({
    "bom-le.xml": utf16(declared("UTF-16"), "le"),
    "bom-be.xml": utf16(declared("UTF-16"), "be"),
    "bare-le.xml": utf16(declared("UTF-16"), "le", false),
    "bare-be.xml": utf16(declared("UTF-16"), "be", false),
    "latin1.xml": Uint8Array.from(declared("ISO-8859-1"), (c) => c.charCodeAt(0)),
    "unknown.xml": strToU8(declared("x-no-such-encoding")),
  });

  const names = ["bom-le.xml", "bom-be.xml", "bare-le.xml", "bare-be.xml", "latin1.xml", "unknown.xml"];
  const result = unzipEntries(zip, { names });

  expect(result).toEqual({
    ok: true,
    inflated: expect.any(Number),
    files: new Map([
      ["bom-le.xml", declared("UTF-16")],
      ["bom-be.xml", declared("UTF-16")],
      ["bare-le.xml", declared("UTF-16")],
      ["bare-be.xml", declared("UTF-16")],
      ["latin1.xml", declared("ISO-8859-1")],
      ["unknown.xml", declared("x-no-such-encoding")],
    ]),
  });
});

test("calls bytes that are not a zip damaged", () => {
  expect(unzipEntries(strToU8("%PDF-1.7 not a zip at all"), { names: ["word/document.xml"] })).toEqual({
    ok: false,
    why: "damaged",
  });
});
