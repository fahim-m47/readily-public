// Text pulled out of a document, settled into the shape the Engine reads: a
// blank line between paragraphs, a single newline where a line merely wraps
// (which the Engine reads as a space), and no runs of blank lines or
// whitespace hanging off either end of a line. Lines are trimmed one by one
// rather than by a regex around each newline, which backtracks quadratically
// over a long run of spaces with no newline to find.
export const tidyText = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

// Parses markup the way the document declares it, or says it could not.
export const parseXml = (source: string, type: DOMParserSupportedType = "application/xml") => {
  const document = new DOMParser().parseFromString(source, type);
  return document.getElementsByTagName("parsererror").length > 0 ? null : document;
};

// Elements whose text is never part of what a page says.
const SKIPPED = new Set(["head", "nav", "noscript", "script", "style", "template"]);
// Elements that stand as a paragraph of their own. A table cell is one too,
// so a row does not run its cells together into one sentence.
const BLOCKS = new Set([
  "address", "article", "aside", "blockquote", "body", "caption", "dd", "details", "div", "dl", "dt",
  "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
  "ol", "p", "pre", "section", "summary", "table", "td", "th", "tr", "ul",
]);

const ROMAN = [
  [1000, "m"], [900, "cm"], [500, "d"], [400, "cd"], [100, "c"], [90, "xc"], [50, "l"],
  [40, "xl"], [10, "x"], [9, "ix"], [5, "v"], [4, "iv"], [1, "i"],
] as const;

// An ordered list's item number written the way a browser renders the
// list's `type`: letters for `a` and `A` (`a` to `z`, then `aa`, `ab`),
// roman numerals for `i` and `I`, and decimal for any other type, as well
// as where a browser falls back to it, a number below one for letters or
// numerals and one past 3999 for numerals.
const numeral = (number: number, type: string | null) => {
  let text = "";
  if ((type === "a" || type === "A") && number > 0) {
    for (let n = number; n > 0; n = Math.floor((n - 1) / 26)) text = String.fromCharCode(97 + ((n - 1) % 26)) + text;
  } else if ((type === "i" || type === "I") && number > 0 && number < 4000) {
    let n = number;
    for (const [value, letters] of ROMAN) {
      while (n >= value) {
        text += letters;
        n -= value;
      }
    }
  } else {
    return String(number);
  }
  return type === "A" || type === "I" ? text.toUpperCase() : text;
};

// The text of an HTML or XHTML tree — an EPUB chapter, or the article
// Readability found on a page — in the shape `tidyText` settles: each block
// element its own paragraph, a `br` a line break, and whitespace collapsed
// the way a browser renders it, except inside `pre`. An ordered list's items
// open with their number the way a numbered Markdown list reads (`1. First`),
// counting from the list's `start` and an item's own `value`, counting
// down from the list's length when it is `reversed`, and written in the
// letters or roman numerals the list's `type`, or an item's own, asks for;
// a bulleted list's items are bare. An image is its `alt` text, inline
// where it sits, the way the Markdown importer reads `![alt](src)`; an
// image with no alt, or a blank one, is decorative and says nothing. A
// `hidden` element is left out as a browser leaves it off the page, unless
// it is hidden only `until-found`, which find-in-page reveals like
// collapsed `details`.
export const blockText = (root: Node) => {
  const out: string[] = [];
  // A numbered item's marker, said just before its first words so an item
  // that opens with a paragraph does not put its number on a line of its own.
  // It stays pending past empty elements and is dropped when its item closes,
  // so an item with no words leaks no number into the next block.
  let marker = "";
  const say = (text: string) => {
    if (marker && /\S/.test(text)) {
      out.push(marker);
      marker = "";
    }
    out.push(text);
  };
  const walk = (node: Node, pre: boolean) => {
    const numbered = node instanceof Element && node.localName === "ol";
    const reversed = numbered && node.hasAttribute("reversed");
    const style = numbered ? node.getAttribute("type") : null;
    const start = numbered ? Number.parseInt(node.getAttribute("start") ?? "", 10) : NaN;
    const first = reversed ? [...node.children].filter((child) => child.localName === "li").length : 1;
    let number = Number.isNaN(start) ? first : start;
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE || child.nodeType === Node.CDATA_SECTION_NODE) {
        const text = child.nodeValue ?? "";
        say(pre ? text : text.replace(/\s+/g, " "));
      } else if (child instanceof Element && !SKIPPED.has(child.localName)) {
        const hidden = child.getAttribute("hidden");
        if (hidden !== null && hidden.toLowerCase() !== "until-found") continue;
        if (child.localName === "br") {
          out.push("\n");
          continue;
        }
        if (child.localName === "img") {
          const alt = child.getAttribute("alt") ?? "";
          if (/\S/.test(alt)) say(pre ? alt : alt.replace(/\s+/g, " "));
          continue;
        }
        const block = BLOCKS.has(child.localName);
        if (block) out.push("\n\n");
        const item = numbered && child.localName === "li";
        if (item) {
          const value = Number.parseInt(child.getAttribute("value") ?? "", 10);
          if (!Number.isNaN(value)) number = value;
          marker = `${numeral(number, child.getAttribute("type") ?? style)}. `;
          number += reversed ? -1 : 1;
        }
        walk(child, pre || child.localName === "pre");
        if (item) marker = "";
        if (block) out.push("\n\n");
      }
    }
  };
  walk(root, false);
  return tidyText(out.join("").replace(/[^\S\n]+/g, " "));
};
