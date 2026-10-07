import "./promiseWithResolvers";
import { SOURCE_FILE_BYTE_LIMIT, type Extracted } from "../importSource";
import { tidyText } from "./blockText";

// A run of text as pdf.js reports it, or a marked-content marker, which
// carries no text.
type Run = { str: string; hasEOL: boolean; transform: number[] };
type Line = { text: string; baseline: number };
// The items of one chunk of a page's text, as pdf.js streams them.
type TextChunk = { items: Array<Run | { type: string }> };

// How much wider than a page's usual line spacing a gap must be to count as
// a paragraph break.
const PARAGRAPH_GAP = 1.3;

// One page's text, folded from pdf.js's chunks as they arrive: `add` each
// chunk's items, then `text` once for the page. A line ends wherever pdf.js
// saw it end, and a gap between lines clearly wider than the page's usual
// spacing starts a new paragraph. "Usual" is measured on the page itself
// (the lower median of its gaps), so a double-spaced page reads as wrapped
// lines rather than a paragraph a line. Only lines with text are kept, so a
// page holds its text, not every run pdf.js reported.
export const foldPage = () => {
  const lines: Line[] = [];
  let current: Line | undefined;
  const close = () => {
    if (current && current.text.trim() !== "") lines.push(current);
    current = undefined;
  };
  return {
    add(items: TextChunk["items"]) {
      for (const item of items) {
        if (!("str" in item)) continue;
        current ??= { text: "", baseline: item.transform[5] };
        current.text += item.str;
        if (item.hasEOL) close();
      }
    },
    text() {
      close();
      const gaps = lines.slice(1).map((line, index) => lines[index].baseline - line.baseline);
      const spacings = gaps.filter((gap) => gap > 0).sort((a, b) => a - b);
      const usual = spacings[Math.floor((spacings.length - 1) / 2)] ?? 0;
      return lines
        .map((line, index) => {
          if (index === 0) return line.text;
          return (gaps[index - 1] > usual * PARAGRAPH_GAP ? "\n\n" : "\n") + line.text;
        })
        .join("");
    },
  };
};

// The text of a PDF, page by page, a blank line between pages. Only text is
// extracted: nothing is rendered, no script runs, and no character maps are
// shipped, so a PDF that relies on Adobe's predefined CJK maps may yield
// little. A PDF that needs a password to open is refused as locked; one with
// only an owner password (printing or copying restrictions) opens as usual.
// pdf.js's legacy build is used so the parser runs on WebKit 17.0 (macOS 14.0).
//
// Reading stops, and the document is refused, once the text pdf.js has handed
// over is more than a Source may hold, so a small file whose streams unpack
// to a vast amount of text costs the limit's worth of work, not the
// document's. Each page's text is pulled from pdf.js in chunks and counted as
// it arrives, every character included, so one page with a vast stream stops
// partway through the page rather than after it. Destroying the task tears
// the page's stream down with it; nothing further is pulled. Each chunk is
// folded into the page's lines on arrival, so memory follows the text kept,
// not the count of runs pdf.js reported.
export const readPdf = async (file: File): Promise<Extracted> => {
  const { getDocument, PDFWorker, PasswordException } = await import("pdfjs-dist/legacy/build/pdf.mjs");
  const data = new Uint8Array(await file.arrayBuffer());
  const port = new Worker(new URL("./pdf.worker.ts", import.meta.url), { type: "module" });
  let worker: ReturnType<typeof PDFWorker.create> | undefined;
  let task: ReturnType<typeof getDocument> | undefined;
  try {
    worker = PDFWorker.create({ port });
    task = getDocument({ data, worker });
    const pdf = await task.promise;
    const pages: string[] = [];
    let length = 0;
    for (let number = 1; number <= pdf.numPages; number++) {
      const page = await pdf.getPage(number);
      const folded = foldPage();
      // A reader rather than `for await`, which WebKit 17.0 lacks on streams.
      const reader: ReadableStreamDefaultReader<TextChunk> = page.streamTextContent().getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const item of value.items) {
          if ("str" in item) length += item.str.length;
        }
        if (length > SOURCE_FILE_BYTE_LIMIT) return { ok: false, why: "too-big" };
        folded.add(value.items);
      }
      page.cleanup();
      pages.push(folded.text());
    }
    return { ok: true, text: tidyText(pages.join("\n\n")) };
  } catch (error) {
    if (error instanceof PasswordException) return { ok: false, why: "locked" };
    throw error;
  } finally {
    await task?.destroy();
    worker?.destroy();
    port.terminate();
  }
};
