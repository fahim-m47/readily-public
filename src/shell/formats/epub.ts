import { SOURCE_FILE_BYTE_LIMIT, type Extracted } from "../importSource";
import { blockText, parseXml } from "./blockText";
import { unzip } from "./unzip";

const CONTAINER = "META-INF/container.xml";
const ENCRYPTION = "META-INF/encryption.xml";
const CHAPTER_TYPES = new Set(["application/xhtml+xml", "text/html"]);
// What a spine entry may resolve to: a chapter, or an SVG content document,
// which EPUB 3 lets stand in the spine on its own (an illustrated title
// page) and which holds no text to read.
const CONTENT_TYPES = new Set([...CHAPTER_TYPES, "image/svg+xml"]);
// The two ways a DRM-free book hides its fonts in `encryption.xml`: the
// IDPF's obfuscation and Adobe's. Any other algorithm there is encryption.
const FONT_OBFUSCATION = new Set(["http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"]);

// An archive path from a URL-encoded reference, as zip entries are named:
// `href` relative to the entry at `base`, or to the archive root.
const entryPath = (href: string, base = "") =>
  decodeURIComponent(new URL(href, `https://epub.invalid/${base}`).pathname.slice(1));

const elements = (parent: Document | Element, name: string) => [...parent.getElementsByTagNameNS("*", name)];

// A manifest item: where it lives in the archive, what it is (its media
// type in lower case, since MIME names compare without case), and the item
// the book offers in its place to a reader that cannot show it.
type Item = { path: string; type: string; fallback: string | null };

// A chapter parsed the way a browser would from its manifest type: HTML as
// HTML, and XHTML as XML, falling back to HTML for the books that ship XHTML
// that is not well-formed XML, which a browser still renders.
const parseChapter = (source: string, type: string) =>
  (type === "text/html" ? null : parseXml(source, "application/xhtml+xml")) ??
  new DOMParser().parseFromString(source, "text/html");

// Whether a book's `encryption.xml` locks it: anything encrypted by more than
// font obfuscation, or an `encryption.xml` too broken to tell, whatever the
// resources it names.
const isLocked = (encryption: string | undefined) => {
  if (encryption === undefined) return false;
  const document = parseXml(encryption);
  return (
    !document ||
    elements(document, "EncryptedData").some(
      (data) => !FONT_OBFUSCATION.has(elements(data, "EncryptionMethod")[0]?.getAttribute("Algorithm") ?? ""),
    )
  );
};

// The text of an EPUB: its reading order (the spine) chapter by chapter,
// leaving out what the book marks as outside it, such as a cover. A book
// with anything encrypted is DRM-locked and refused; one that lists only
// obfuscated fonts in `encryption.xml` reads. A spine chapter missing from
// the archive makes the book damaged rather than quietly shorter, and so
// does a spine entry that resolves to no content document through its
// fallback chain, or two entries that resolve to the same content document,
// which EPUB forbids and which would otherwise read a chapter as many times
// as it is named. Refusing the repeat whatever the document's type also
// bounds the fallback walks: each item has one fallback, so two chains that
// overlap end at the same document, and the second is refused. An entry
// that resolves to an SVG document is otherwise skipped: it is in the
// reading order, but there is no text in it to read.
// Reading stops as soon as the text passes what the composer opens, so a
// book with far more text than that costs no more than the limit to refuse.
//
// EPUB types a resource by its manifest entry, not its name, so the archive
// is read by name in three steps, each naming what the last one found: the
// container and encryption list, then the package document, then the
// chapters. Images, fonts and styles are never inflated.
export const readEpub = async (file: File): Promise<Extracted> => {
  // `unzip` takes its bytes with it, so each step reads the file afresh; the
  // inflate cap, though, is one budget across all three steps.
  let inflated = 0;
  const entries = async (names: string[]) => {
    const result = await unzip(new Uint8Array(await file.arrayBuffer()), { names, inflated });
    if (result.ok) inflated = result.inflated;
    return result;
  };
  const parse = (source: string | undefined) => (source === undefined ? null : parseXml(source));

  const meta = await entries([CONTAINER, ENCRYPTION]);
  if (!meta.ok) return meta;
  if (isLocked(meta.files.get(ENCRYPTION))) return { ok: false, why: "locked" };
  const container = parse(meta.files.get(CONTAINER));
  const fullPath = container && elements(container, "rootfile")[0]?.getAttribute("full-path");
  if (!fullPath) return { ok: false, why: "damaged" };
  const packagePath = entryPath(fullPath);

  const pkg = await entries([packagePath]);
  if (!pkg.ok) return pkg;
  const opf = parse(pkg.files.get(packagePath));
  if (!opf) return { ok: false, why: "damaged" };

  const manifest = new Map<string | null, Item>(
    elements(opf, "item").map((item) => [
      item.getAttribute("id"),
      {
        path: entryPath(item.getAttribute("href") ?? "", packagePath),
        type: (item.getAttribute("media-type") ?? "").toLowerCase(),
        fallback: item.getAttribute("fallback"),
      },
    ]),
  );
  // The content document a spine entry stands for: the first item of a
  // content type along its fallback chain, as a reading system takes the
  // first type it supports, or none when the chain ends or loops first.
  const contentOf = (idref: string | null) => {
    const seen = new Set<string | null>();
    for (let id = idref; id !== null && !seen.has(id); id = manifest.get(id)?.fallback ?? null) {
      const item = manifest.get(id);
      if (item && CONTENT_TYPES.has(item.type)) return item;
      seen.add(id);
    }
    return undefined;
  };
  const documents = new Set<string>();
  const chapters = new Map<string, string>();
  for (const itemref of elements(opf, "itemref")) {
    if (itemref.getAttribute("linear") === "no") continue;
    const content = contentOf(itemref.getAttribute("idref"));
    if (content === undefined || documents.has(content.path)) return { ok: false, why: "damaged" };
    documents.add(content.path);
    if (CHAPTER_TYPES.has(content.type)) chapters.set(content.path, content.type);
  }

  const read = await entries([...chapters.keys()]);
  if (!read.ok) return read;
  const text: string[] = [];
  let length = 0;
  for (const [path, type] of chapters) {
    const source = read.files.get(path);
    if (source === undefined) return { ok: false, why: "damaged" };
    const words = blockText(parseChapter(source, type).documentElement);
    if (words === "") continue;
    length += words.length + (text.length > 0 ? 2 : 0);
    if (length > SOURCE_FILE_BYTE_LIMIT) return { ok: false, why: "too-long" };
    text.push(words);
  }
  return { ok: true, text: text.join("\n\n") };
};
