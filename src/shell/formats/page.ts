import { Readability } from "@mozilla/readability";
import type { Extracted } from "../importSource";
import { blockText } from "./blockText";

// The most elements an HTML page may hold and still be read. Readability
// walks the whole tree on the UI thread, and the 4 MiB the Engine allows
// fits over a million three-byte elements, so the count is bounded
// separately: the longest real pages (a 2.9 MB encyclopaedia article, a
// whole novel on one page) hold under 22,000.
export const PAGE_ELEMENT_LIMIT = 50_000;

// How far into an HTML page its `<meta charset>` may be, as the HTML
// standard's encoding sniffing looks for it.
const SNIFF_BYTES = 1024;
// `<meta charset="x">` and the older `<meta http-equiv="Content-Type"
// content="text/html; charset=x">` alike.
const META_CHARSET = /<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)/i;

// A decoder for the label, or UTF-8's when no decoder knows it.
const decoderFor = (label: string | undefined) => {
  try {
    return new TextDecoder(label ?? "utf-8");
  } catch {
    return new TextDecoder();
  }
};

// The charset the page names: in its Content-Type, or for HTML in a meta
// tag near its start, read as bytes before anything is decoded.
const charsetOf = (bytes: Uint8Array, mediaType: string, parameters: string[]) => {
  const header = parameters.find((parameter) => /^charset=/i.test(parameter))?.slice(8);
  if (header || mediaType !== "text/html") return header;
  return META_CHARSET.exec(new TextDecoder("windows-1252").decode(bytes.subarray(0, SNIFF_BYTES)))?.[1];
};

// The text of a page the Engine fetched for "Open link", given its bytes and
// the Content-Type it arrived with (`text/html` or `text/plain`, as
// `POST /v1/sources/fetch` promises). Empty when the page has nothing to
// read, and `too-big` when it is built of more elements than
// `PAGE_ELEMENT_LIMIT`.
//
// Plain text reads the way a `.txt` file does. HTML is parsed into a detached
// document, which runs no script and loads nothing, and Readability picks out
// the article from the navigation, adverts and footer around it; its title
// comes first, as a book's does (threat model, B4).
export const readPage = (bytes: Uint8Array, contentType: string): Extracted => {
  const [mediaType, ...parameters] = contentType.split(";").map((part) => part.trim());
  const text = decoderFor(charsetOf(bytes, mediaType.toLowerCase(), parameters)).decode(bytes);
  if (mediaType.toLowerCase() !== "text/html") return { ok: true, text: text.replace(/\r\n?/g, "\n") };

  const document = new DOMParser().parseFromString(text, "text/html");
  if (document.getElementsByTagName("*").length > PAGE_ELEMENT_LIMIT) return { ok: false, why: "too-big" };
  const article = new Readability(document, { serializer: (node) => node }).parse();
  if (!article?.content) return { ok: true, text: "" };
  const body = blockText(article.content);
  const title = article.title?.trim();
  return { ok: true, text: !title || !body || body.startsWith(title) ? body : `${title}\n\n${body}` };
};
