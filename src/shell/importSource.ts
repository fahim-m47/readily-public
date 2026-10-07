// A file a reader drops on the composer or picks with "Open file", or a link
// they give "Open link", read into a Source.
//
// The file arrives as a browser `File` the OS handed the webview, so its
// bytes are all Readily sees: no path, and nothing here asks Rust or the
// Engine to read one (threat model, B4). A link's page is the one thing the
// Engine fetches for the webview, as bytes it does not look inside. Every
// parser runs in the webview, and each is loaded only when a file of its
// format, or a page, arrives.
import type { EngineClient, FetchedPage } from "../engine/client";
import { formatBytes } from "./estimate";

// The largest text or Markdown file the composer will read, checked against
// its size before a byte is read. Comfortably above the 1,000,000-character
// Source limit in UTF-8 (at most four bytes a character), and small enough
// that the code-point spread in `estimateSource` cannot freeze the UI. A file
// between the two reads in and says, in the estimate's own line, how far over
// it is. The same ceiling applies to the text a document yields.
export const SOURCE_FILE_BYTE_LIMIT = 8 * 1024 * 1024;

// The largest PDF, EPUB or Word file the composer will read. Documents carry
// images and fonts the text is a small part of, so the file may be far bigger
// than the text it holds.
export const DOCUMENT_BYTE_LIMIT = 100 * 1024 * 1024;

export type ImportResult = { ok: true; text: string } | { ok: false; reason: string };

// What a format's reader makes of a file: its text, which may be empty, or
// why there is none to be had.
export type Extracted =
  | { ok: true; text: string }
  | { ok: false; why: "damaged" | "too-big" | "too-long" | "locked" };

type Format = {
  // The first is the one named to readers.
  extensions: string[];
  types: string[];
  byteLimit: number;
  read: (file: File) => Promise<Extracted>;
  // Said after "<name> has no text in it." when a file of this format is
  // empty, where there is a likely reason worth giving.
  whyEmpty?: string;
};

const readText = async (file: File): Promise<Extracted> => ({
  ok: true,
  text: (await file.text()).replace(/\r\n?/g, "\n"),
});

// Every format the composer opens, in the order readers see them listed.
const FORMATS: Format[] = [
  { extensions: [".txt"], types: ["text/plain"], byteLimit: SOURCE_FILE_BYTE_LIMIT, read: readText },
  {
    extensions: [".md", ".markdown"],
    types: ["text/markdown"],
    byteLimit: SOURCE_FILE_BYTE_LIMIT,
    read: async (file) => {
      const read = await readText(file);
      return read.ok ? { ok: true, text: tidyMarkdown(read.text) } : read;
    },
  },
  {
    extensions: [".docx"],
    types: ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
    byteLimit: DOCUMENT_BYTE_LIMIT,
    read: async (file) => (await import("./formats/docx")).readDocx(file),
  },
  {
    extensions: [".epub"],
    types: ["application/epub+zip"],
    byteLimit: DOCUMENT_BYTE_LIMIT,
    read: async (file) => (await import("./formats/epub")).readEpub(file),
  },
  {
    extensions: [".pdf"],
    types: ["application/pdf"],
    byteLimit: DOCUMENT_BYTE_LIMIT,
    read: async (file) => (await import("./formats/pdf")).readPdf(file),
    whyEmpty: "It may be a scan, which holds pictures of pages rather than text.",
  },
];

const names = FORMATS.map((format) => format.extensions[0]);
const listed = (conjunction: string) => `${names.slice(0, -1).join(", ")} ${conjunction} ${names.at(-1)}`;

// For the file picker's `accept`.
export const SOURCE_FILE_ACCEPT = FORMATS.flatMap((format) => format.extensions).join(",");
// The formats as a reader-facing list, for "a … file".
export const SOURCE_FILE_KINDS = listed("or");

const formatOf = (file: File) => {
  const extension = /\.[^.]*$/.exec(file.name.toLowerCase())?.[0] ?? "";
  return (
    FORMATS.find((format) => format.extensions.includes(extension)) ??
    FORMATS.find((format) => format.types.includes(file.type))
  );
};

// Why a file of a readable format still yielded no text, for the reader.
const REFUSALS: Record<Extract<Extracted, { ok: false }>["why"], (name: string) => string> = {
  damaged: (name) => `Readily couldn't read ${name}.`,
  "too-big": (name) => `${name} unpacks to more than Readily opens.`,
  "too-long": (name) => `${name} holds more text than Readily opens at once.`,
  locked: (name) => `${name} is locked with a password or DRM, so Readily can't read it.`,
};

// Refuses a file the composer cannot take — not a format it reads, too big,
// damaged, locked, or empty once read — with a sentence the reader can act
// on, and otherwise returns the text the draft should become.
//
// A text or Markdown file is decoded as UTF-8, which is what `File.text()`
// does; one in a legacy encoding (Latin-1, UTF-16) is not supported and reads
// with replacement characters. Line endings come back as `\n`, the way a textarea
// would hold the same text pasted.
export const readSourceFile = async (file: File): Promise<ImportResult> => {
  const format = formatOf(file);
  if (!format) {
    return { ok: false, reason: `Readily opens ${listed("and")} files, and ${file.name} is none of these.` };
  }
  if (file.size > format.byteLimit) {
    return {
      ok: false,
      reason: `${file.name} is ${formatBytes(file.size)}, more than the ${formatBytes(format.byteLimit)} Readily opens.`,
    };
  }

  let extracted: Extracted;
  try {
    extracted = await format.read(file);
  } catch {
    extracted = { ok: false, why: "damaged" };
  }
  if (!extracted.ok) return { ok: false, reason: REFUSALS[extracted.why](file.name) };
  return settle(file.name, extracted.text, format.whyEmpty);
};

// A scheme at the start of what was typed. A colon followed by a number and
// then the end of the host (`example.com:8443/article`) is a port, not one.
const SCHEME = /^[a-z][a-z\d+.-]*:(?!\d+(?:[/?#]|$))/i;

// What a reader typed as a link, as the URL a browser would take it for: one
// with no scheme is https. Null when it is not a URL at all.
const linkUrl = (link: string) => {
  const typed = link.trim();
  try {
    return new URL(SCHEME.test(typed) ? typed : `https://${typed}`);
  } catch {
    return null;
  }
};

// Reads the page behind a link into a Source, refusing what the Engine
// would not fetch or the page does not hold the way `readSourceFile` refuses
// a file. `fetchPage` is the Engine's; its refusals are already sentences
// for the reader, and are passed on as they are. Only ever called from a
// reader's explicit "Open link", never on paste (threat model, egress
// inventory row 5).
export const readSourceLink = async (
  link: string,
  fetchPage: EngineClient["fetchPage"],
): Promise<ImportResult> => {
  const url = linkUrl(link);
  if (!url) return { ok: false, reason: `“${link.trim()}” is not a link Readily can open.` };

  let page: FetchedPage;
  try {
    page = await fetchPage(url.href);
  } catch (error) {
    return { ok: false, reason: error instanceof Error ? error.message : `Readily couldn't reach ${url.hostname}.` };
  }
  let read: Extracted;
  try {
    read = (await import("./formats/page")).readPage(page.bytes, page.contentType);
  } catch {
    read = { ok: false, why: "damaged" };
  }
  if (!read.ok) {
    return {
      ok: false,
      reason:
        read.why === "too-big"
          ? `The page at ${url.hostname} is larger than Readily opens.`
          : REFUSALS[read.why](url.hostname),
    };
  }
  return settle(url.hostname, read.text);
};

// The text a file or page yielded as the draft, or why it cannot be: it is
// empty, with the format's likely reason when it has one, or longer than the
// composer takes.
const settle = (name: string, text: string, whyEmpty?: string): ImportResult => {
  if (text.trim() === "") {
    return { ok: false, reason: [`${name} has no text in it.`, whyEmpty].filter(Boolean).join(" ") };
  }
  if (text.length > SOURCE_FILE_BYTE_LIMIT) return { ok: false, reason: REFUSALS["too-long"](name) };
  return { ok: true, text };
};

// A fence line's indentation and marker run. An opening fence may carry an
// info string (```ts); a closing one is the marker run alone, and closes
// only within three spaces of its opener's indentation.
const FENCE = /^(\s*)(`{3,}|~{3,})/;
const FENCE_CLOSE = /^(\s*)(`{3,}|~{3,})\s*$/;
const RULE = /^ {0,3}([-*_])( *\1){2,} *$/;
const SETEXT_UNDERLINE = /^ {0,3}(=+|-+) *$/;
const QUOTE = /^ {0,3}(> ?)+/;
// A heading's opening hashes, and its optional closing ones. Two patterns
// rather than one around a lazy `(.*?)`, which backtracks cubically on a
// long line that cannot match.
const HEADING_OPEN = /^ {0,3}#{1,6}(?: +|$)/;
const HEADING_CLOSE = /(?:^| )#+ *$/;
// Bullets only: an ordered list's number is content a Voice should read.
const BULLET = /^\s*[-*+] +/;
// Every inline pattern stops at the next delimiter of its own kind, so an
// opener that never closes costs a scan to that delimiter, not to the end of
// the line; the cap on a file's size is only a cap on work if each opener's
// scan is bounded. Link text holds no brackets and a destination one level
// of balanced parentheses, as a Wikipedia URL often does. Emphasis holds no
// marker of its own kind, so emphasis nested inside strong keeps its markers.
const IMAGE = /!\[([^[\]]*)\]\((?:[^()]|\([^()]*\))*\)/g;
const LINK = /\[([^[\]]+)\]\((?:[^()]|\([^()]*\))*\)/g;
const STRONG = /(?<![\w*])(?:\*\*(?=\S)([^*\n]+)(?<=\S)\*\*|__(?=\S)([^_\n]+)(?<=\S)__)(?![\w*])/g;
const EMPHASIS = /(?<![\w*])(?:\*(?=\S)([^*\n]+)(?<=\S)\*|_(?=\S)([^_\n]+)(?<=\S)_)(?![\w*])/g;
const STRIKE = /~~(?=\S)([^~\n]+)(?<=\S)~~/g;

// A heading line's words, without its hashes; any other line as it is.
const unheading = (line: string) => {
  const words = line.replace(HEADING_OPEN, "");
  return words === line ? line : words.replace(HEADING_CLOSE, "").trimEnd();
};

// Takes the Markdown markup out of a file's text so a Voice does not read it
// aloud, one line at a time: headings, quotes and bulleted items keep their
// words and lose their markers, numbered items keep their numbers, links and
// images become their text and alt, and emphasis loses its asterisks,
// underscores and tildes. Fence lines, setext underlines and horizontal rules
// go entirely; a fenced block's own lines are kept as written, and the block
// ends only at a bare fence of its opener's character, at least its length
// and within three spaces of its indentation, so a `~~~`, ````x or
// indented ``` line inside a ``` block is code, not a close.
//
// A tidy-up for text-to-speech, not a Markdown parser: nothing is fetched,
// no HTML is built, and the DOM is never touched. Markup it does not
// recognise stays in the text, so a stray marker read aloud is the accepted
// failure mode; emphasis nested inside strong (`**a *b* c**`) is one.
export const tidyMarkdown = (text: string) => {
  // The open block's fence and its indentation, or "" outside one.
  let fence = "";
  let indent = 0;
  const lines: string[] = [];
  for (const line of text.split("\n")) {
    if (fence) {
      const close = FENCE_CLOSE.exec(line);
      const closes =
        close !== null &&
        close[2][0] === fence[0] &&
        close[2].length >= fence.length &&
        close[1].length <= indent + 3;
      if (closes) fence = "";
      else lines.push(line);
      continue;
    }
    const open = FENCE.exec(line);
    if (open) {
      indent = open[1].length;
      fence = open[2];
      continue;
    }
    if (RULE.test(line) || SETEXT_UNDERLINE.test(line)) continue;

    lines.push(
      unheading(line.replace(QUOTE, ""))
        .replace(BULLET, "")
        .replace(IMAGE, "$1")
        .replace(LINK, "$1")
        .replace(STRONG, "$1$2")
        .replace(EMPHASIS, "$1$2")
        .replace(STRIKE, "$1"),
    );
  }
  return lines.join("\n");
};
