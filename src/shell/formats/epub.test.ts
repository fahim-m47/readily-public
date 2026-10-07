import { strToU8, zipSync } from "fflate";
import { expect, test, vi } from "vitest";
import { SOURCE_FILE_BYTE_LIMIT } from "../importSource";
import { readEpub } from "./epub";
import { INFLATE_CAP } from "./unzip";

// jsdom has no Worker, so the unzip runs in-thread.
vi.mock("./unzip", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./unzip")>();
  return { ...actual, unzip: async (bytes: Uint8Array, want: import("./unzip").Want) => actual.unzipEntries(bytes, want) };
});

const CONTAINER = `<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>`;

// A package whose spine reads the chapters in a different order from the
// manifest, keeps a cover out of the reading order, and names one chapter
// with a percent-encoded space.
const OPF = `<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="cover" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="two" href="Text/chapter%202.xhtml" media-type="application/xhtml+xml"/>
    <item id="one" href="Text/one.xhtml" media-type="application/xhtml+xml"/>
    <item id="font" href="Fonts/serif.otf" media-type="font/otf"/>
  </manifest>
  <spine>
    <itemref idref="cover" linear="no"/>
    <itemref idref="one"/>
    <itemref idref="two"/>
  </spine>
</package>`;

// A package of the given manifest items and spine, for the books OPF cannot
// be edited into.
const pkg = (manifest: string, spine: string) =>
  `<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>${manifest}</manifest><spine>${spine}</spine></package>`;
const item = (id: string, href: string, attributes = 'media-type="application/xhtml+xml"') =>
  `<item id="${id}" href="${href}" ${attributes}/>`;
const itemref = (idref: string, attributes = "") => `<itemref idref="${idref}" ${attributes}/>`;

const chapter = (body: string) =>
  `<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head><body>${body}</body></html>`;

// An `encryption.xml` listing one resource, encrypted the way DRM does
// unless an obfuscation algorithm is named.
const encryption = (uri: string, algorithm = "http://www.w3.org/2001/04/xmlenc#aes128-cbc") => `<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="${algorithm}"/>
    <enc:CipherData><enc:CipherReference URI="${uri}"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>`;
const IDPF_FONT_OBFUSCATION = "http://www.idpf.org/2008/embedding";

// `text` as UTF-16LE behind a byte-order mark, the other encoding EPUB
// allows its XML to be written in.
const utf16le = (text: string) => {
  const bytes = new Uint8Array(2 + text.length * 2);
  const view = new DataView(bytes.buffer);
  view.setUint16(0, 0xfeff, true);
  for (let i = 0; i < text.length; i += 1) view.setUint16(2 + i * 2, text.charCodeAt(i), true);
  return bytes;
};

const epub = (entries: Record<string, string | Uint8Array>) =>
  new File(
    [zipSync(Object.fromEntries(Object.entries(entries).map(([name, text]) => [name, typeof text === "string" ? strToU8(text) : text])))],
    "book.epub",
  );

const book = (extra: Record<string, string> = {}) =>
  epub({
    mimetype: "application/epub+zip",
    "META-INF/container.xml": CONTAINER,
    "OEBPS/content.opf": OPF,
    "OEBPS/nav.xhtml": chapter("<nav><ol><li>Contents</li></ol></nav>"),
    "OEBPS/Text/cover.xhtml": chapter("<p>Cover</p>"),
    "OEBPS/Text/one.xhtml": chapter("<h1>The Lighthouse</h1><p>The keeper climbed the stairs.</p>"),
    "OEBPS/Text/chapter 2.xhtml": chapter("<p>Ships passed in the night.</p>"),
    "OEBPS/Fonts/serif.otf": "font bytes",
    ...extra,
  });

test("reads the chapters in spine order, leaving out what is outside the reading order", async () => {
  expect(await readEpub(book())).toEqual({
    ok: true,
    text: "The Lighthouse\n\nThe keeper climbed the stairs.\n\nShips passed in the night.",
  });
});

test("refuses a book whose chapters are encrypted", async () => {
  expect(await readEpub(book({ "META-INF/encryption.xml": encryption("OEBPS/Text/one.xhtml") }))).toEqual({
    ok: false,
    why: "locked",
  });
});

test("refuses encryption whatever it names, even a path that matches no chapter", async () => {
  expect(await readEpub(book({ "META-INF/encryption.xml": encryption("Text/one.xhtml") }))).toEqual({
    ok: false,
    why: "locked",
  });
});

test("refuses a book whose encryption.xml cannot be read", async () => {
  expect(await readEpub(book({ "META-INF/encryption.xml": "<encryption><unclosed>" }))).toEqual({
    ok: false,
    why: "locked",
  });
});

test("reads a book that only obfuscates its fonts", async () => {
  const read = await readEpub(
    book({ "META-INF/encryption.xml": encryption("OEBPS/Fonts/serif.otf", IDPF_FONT_OBFUSCATION) }),
  );

  expect(read.ok).toBe(true);
});

test("treats a book missing a chapter of its reading order as damaged", async () => {
  const opf = OPF.replace('href="Text/one.xhtml"', 'href="Text/missing.xhtml"');

  expect(await readEpub(book({ "OEBPS/content.opf": opf }))).toEqual({ ok: false, why: "damaged" });
});

test("refuses a spine that reads the same chapter twice", async () => {
  const opf = OPF.replace('<itemref idref="two"/>', '<itemref idref="two"/><itemref idref="one"/>');

  expect(await readEpub(book({ "OEBPS/content.opf": opf }))).toEqual({ ok: false, why: "damaged" });
});

test("stops reading a book once its text passes what the composer opens", async () => {
  const parse = vi.spyOn(DOMParser.prototype, "parseFromString");
  const long = chapter(`<p>${"a".repeat(SOURCE_FILE_BYTE_LIMIT / 2 + 1)}</p>`);
  const opf = pkg(
    item("one", "one.xhtml") + item("two", "two.xhtml") + item("three", "three.xhtml"),
    itemref("one") + itemref("two") + itemref("three"),
  );
  const chapters = { "OEBPS/one.xhtml": long, "OEBPS/two.xhtml": long, "OEBPS/three.xhtml": long };

  const read = await readEpub(book({ "OEBPS/content.opf": opf, ...chapters }));

  expect(read).toEqual({ ok: false, why: "too-long" });
  expect(parse.mock.calls.filter(([source]) => source.includes("<html")).length).toBe(2);
  parse.mockRestore();
});

test("refuses a book whose entries pass the inflate cap together, though none does alone", async () => {
  const padding = `<!--${" ".repeat(INFLATE_CAP / 2 + 1)}-->`;
  const opf = pkg(item("one", "one.xhtml"), itemref("one")) + padding;

  const read = await readEpub(book({ "OEBPS/content.opf": opf, "OEBPS/one.xhtml": chapter("<p>One.</p>") + padding }));

  expect(read).toEqual({ ok: false, why: "too-big" });
});

test("treats a spine entry that names no manifest item as damaged", async () => {
  const opf = OPF.replace('<itemref idref="two"/>', '<itemref idref="two"/><itemref idref="missing"/>');

  expect(await readEpub(book({ "OEBPS/content.opf": opf }))).toEqual({ ok: false, why: "damaged" });
});

test("reads the chapter a foreign spine entry falls back to", async () => {
  const opf = pkg(
    item("one", "one.xhtml") +
      item("chart", "two.pdf", 'media-type="application/pdf" fallback="widget"') +
      item("widget", "two.custom", 'media-type="application/x-custom" fallback="two"') +
      item("two", "two.xhtml"),
    itemref("one") + itemref("chart"),
  );
  const entries = { "OEBPS/one.xhtml": chapter("<p>One.</p>"), "OEBPS/two.pdf": "%PDF", "OEBPS/two.custom": "?" };

  const read = await readEpub(book({ "OEBPS/content.opf": opf, ...entries, "OEBPS/two.xhtml": chapter("<p>Two.</p>") }));

  expect(read).toEqual({ ok: true, text: "One.\n\nTwo." });
});

test("reads past an SVG title page in the spine without inflating it", async () => {
  const opf = pkg(item("title", "title.svg", 'media-type="image/svg+xml"') + item("one", "one.xhtml"), itemref("title") + itemref("one"));
  const read = await readEpub(book({ "OEBPS/content.opf": opf, "OEBPS/title.svg": "<svg/>", "OEBPS/one.xhtml": chapter("<p>One.</p>") }));

  expect(read).toEqual({ ok: true, text: "One." });
});

test("stops at an SVG content document even when it names a chapter as its fallback", async () => {
  const opf = pkg(
    item("title", "title.svg", 'media-type="image/svg+xml" fallback="one"') + item("one", "one.xhtml") + item("two", "two.xhtml"),
    itemref("title") + itemref("one") + itemref("two"),
  );
  const entries = { "OEBPS/title.svg": "<svg/>", "OEBPS/one.xhtml": chapter("<p>One.</p>"), "OEBPS/two.xhtml": chapter("<p>Two.</p>") };

  expect(await readEpub(book({ "OEBPS/content.opf": opf, ...entries }))).toEqual({ ok: true, text: "One.\n\nTwo." });
});

test("refuses a spine whose entries fall back to the same SVG document twice", async () => {
  const opf = pkg(
    item("chart", "chart.pdf", 'media-type="application/pdf" fallback="map"') +
      item("plan", "plan.pdf", 'media-type="application/pdf" fallback="map"') +
      item("map", "map.svg", 'media-type="image/svg+xml"') +
      item("one", "one.xhtml"),
    itemref("chart") + itemref("plan") + itemref("one"),
  );
  const entries = { "OEBPS/chart.pdf": "%PDF", "OEBPS/plan.pdf": "%PDF", "OEBPS/map.svg": "<svg/>", "OEBPS/one.xhtml": chapter("<p>One.</p>") };

  expect(await readEpub(book({ "OEBPS/content.opf": opf, ...entries }))).toEqual({ ok: false, why: "damaged" });
});

test("treats a fallback chain that never reaches a content document as damaged, even a circular one", async () => {
  const opf = pkg(
    item("a", "a.pdf", 'media-type="application/pdf" fallback="b"') + item("b", "b.custom", 'media-type="application/x-custom" fallback="a"'),
    itemref("a"),
  );

  expect(await readEpub(book({ "OEBPS/content.opf": opf, "OEBPS/a.pdf": "%PDF", "OEBPS/b.custom": "?" }))).toEqual({
    ok: false,
    why: "damaged",
  });
});

test("skips an entry outside the reading order even when it resolves to no chapter", async () => {
  const opf = pkg(item("one", "one.xhtml") + item("map", "map.svg", 'media-type="image/svg+xml"'), itemref("one") + itemref("map", 'linear="no"'));

  expect(await readEpub(book({ "OEBPS/content.opf": opf, "OEBPS/one.xhtml": chapter("<p>One.</p>"), "OEBPS/map.svg": "<svg/>" }))).toEqual({
    ok: true,
    text: "One.",
  });
});

test("reads a chapter whose media type is spelled in capitals, as MIME allows", async () => {
  const opf = pkg(item("one", "one.xhtml", 'media-type="Application/XHTML+XML"'), itemref("one"));

  expect(await readEpub(book({ "OEBPS/content.opf": opf, "OEBPS/one.xhtml": chapter("<p>One.</p>") }))).toEqual({
    ok: true,
    text: "One.",
  });
});

test("reads a chapter the manifest calls HTML as a browser would, whatever the case of its tags", async () => {
  const opf = pkg(item("one", "one.html", 'media-type="text/html"'), itemref("one"));
  const html = "<html><body><P>One</P><P>Two</P></body></html>";

  expect(await readEpub(book({ "OEBPS/content.opf": opf, "OEBPS/one.html": html }))).toEqual({ ok: true, text: "One\n\nTwo" });
});

test("reads a chapter written as HTML rather than well-formed XHTML", async () => {
  const read = await readEpub(book({ "OEBPS/Text/one.xhtml": "<html><body><p>Tide&nbsp;tables<br>and charts</body></html>" }));

  expect(read).toEqual({ ok: true, text: "Tide tables\nand charts\n\nShips passed in the night." });
});

test("reads the entries a book names, whatever they end in", async () => {
  const container = CONTAINER.replace("OEBPS/content.opf", "OEBPS/book.package");
  const opf = pkg(item("one", "Text/chapter1"), itemref("one"));

  const read = await readEpub(
    epub({
      mimetype: "application/epub+zip",
      "META-INF/container.xml": container,
      "OEBPS/book.package": opf,
      "OEBPS/Text/chapter1": chapter("<p>One.</p>"),
    }),
  );

  expect(read).toEqual({ ok: true, text: "One." });
});

test("reads a book whose package path is percent-encoded, as EPUB 3 writes it", async () => {
  const container = CONTAINER.replace("OEBPS/content.opf", "OEBPS/My%20Book.opf");
  const opf = pkg(item("one", "one.xhtml"), itemref("one"));

  const read = await readEpub(
    epub({
      mimetype: "application/epub+zip",
      "META-INF/container.xml": container,
      "OEBPS/My Book.opf": opf,
      "OEBPS/one.xhtml": chapter("<p>One.</p>"),
    }),
  );

  expect(read).toEqual({ ok: true, text: "One." });
});

test("reads a book whose package path is spelled with a literal space, as EPUB 2 books are", async () => {
  const container = CONTAINER.replace("OEBPS/content.opf", "OEBPS/My Book.opf");
  const opf = pkg(item("one", "one.xhtml"), itemref("one"));

  const read = await readEpub(
    epub({
      mimetype: "application/epub+zip",
      "META-INF/container.xml": container,
      "OEBPS/My Book.opf": opf,
      "OEBPS/one.xhtml": chapter("<p>One.</p>"),
    }),
  );

  expect(read).toEqual({ ok: true, text: "One." });
});

test("reads a book whose XML is written in UTF-16", async () => {
  const read = await readEpub(
    epub({
      mimetype: "application/epub+zip",
      "META-INF/container.xml": utf16le(CONTAINER),
      "OEBPS/content.opf": utf16le(pkg(item("one", "one.xhtml"), itemref("one"))),
      "OEBPS/one.xhtml": utf16le(chapter("<p>Où est la mer ?</p>").replace('encoding="UTF-8"', 'encoding="UTF-16"')),
    }),
  );

  expect(read).toEqual({ ok: true, text: "Où est la mer ?" });
});

test("treats a zip with no package document as damaged", async () => {
  expect(await readEpub(epub({ mimetype: "application/epub+zip", "OEBPS/one.xhtml": chapter("<p>Hi</p>") }))).toEqual({
    ok: false,
    why: "damaged",
  });
});
