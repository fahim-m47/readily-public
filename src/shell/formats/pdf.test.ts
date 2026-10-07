import { expect, test, vi } from "vitest";
import { foldPage, readPdf } from "./pdf";

// jsdom has no Worker, and pdf.js needs a real browser to parse anything;
// its reader is exercised there. Here the worker is a stub that remembers
// whether it was terminated, and each test decides what pdf.js does.
class StubWorker {
  static terminated = 0;
  terminate() {
    StubWorker.terminated += 1;
  }
}
vi.stubGlobal("Worker", StubWorker);
const pdfjs = vi.hoisted(() => ({ getDocument: vi.fn(), createWorker: vi.fn() }));
vi.mock("pdfjs-dist/legacy/build/pdf.mjs", () => ({
  getDocument: pdfjs.getDocument,
  PDFWorker: { create: pdfjs.createWorker },
  PasswordException: class {},
}));

// A run of text as pdf.js reports it: its string, its baseline's height up
// the page, its font size, and whether pdf.js saw its line end there.
const run = (str: string, y: number, hasEOL = true, height = 12) => ({
  str,
  hasEOL,
  height,
  transform: [height, 0, 0, height, 72, y],
});

test("terminates the worker thread it spawned when pdf.js fails before a task exists", async () => {
  pdfjs.createWorker.mockImplementation(() => {
    throw new Error("no worker here");
  });

  await expect(readPdf(new File(["%PDF-1.7"], "a.pdf"))).rejects.toThrow("no worker here");

  expect(StubWorker.terminated).toBe(1);
});

// A page's text as pdf.js streams it: one chunk of runs per pull, handed
// over only when the reader asks (no read-ahead), so the pulls made are
// exactly the chunks the reader consumed. Cancelling holds the reader to
// pdf.js's contract: its stream asserts the reason is an Error.
const streamed = (chunks: Array<ReturnType<typeof run>[]>) => {
  let pulled = 0;
  const stream = new ReadableStream(
    {
      pull(controller) {
        controller.enqueue({ items: chunks[pulled], styles: {} });
        pulled += 1;
        if (pulled === chunks.length) controller.close();
      },
      cancel(reason) {
        if (!(reason instanceof Error)) throw new Error("cancel must have a valid reason");
      },
    },
    { highWaterMark: 0 },
  );
  return { stream, pulled: () => pulled };
};

// A document pdf.js opens whose every page is `page`.
const opened = (page: { streamTextContent: () => ReadableStream; cleanup: () => void }, numPages: number) => {
  const pdf = { numPages, getPage: vi.fn(async () => page) };
  const task = { promise: Promise.resolve(pdf), destroy: vi.fn(async () => {}) };
  const worker = { destroy: vi.fn() };
  pdfjs.createWorker.mockReturnValue(worker);
  pdfjs.getDocument.mockReturnValue(task);
  StubWorker.terminated = 0;
  return { task, worker };
};

test("refuses a document once its pages' text is already more than the composer opens", async () => {
  const pageText = "a".repeat(5_000_000);
  const page = { streamTextContent: vi.fn(() => streamed([[run(pageText, 700)]]).stream), cleanup: vi.fn() };
  const { task, worker } = opened(page, 6);

  const read = await readPdf(new File(["%PDF-1.7"], "a.pdf"));

  expect(page.streamTextContent).toHaveBeenCalledTimes(2);
  expect(read).toEqual({ ok: false, why: "too-big" });
  expect(task.destroy).toHaveBeenCalledOnce();
  expect(worker.destroy).toHaveBeenCalledOnce();
  expect(StubWorker.terminated).toBe(1);
});

test("stops pulling a page's text mid-page and refuses once the chunks so far are more than the composer opens", async () => {
  const third = "a".repeat(3_000_000);
  const source = streamed([[run(third, 700)], [run(third, 688)], [run(third, 676)], [run("never pulled", 664)]]);
  const page = { streamTextContent: vi.fn(() => source.stream), cleanup: vi.fn() };
  const { task, worker } = opened(page, 1);

  const read = await readPdf(new File(["%PDF-1.7"], "a.pdf"));

  expect(source.pulled()).toBe(3);
  expect(read).toEqual({ ok: false, why: "too-big" });
  expect(task.destroy).toHaveBeenCalledOnce();
  expect(worker.destroy).toHaveBeenCalledOnce();
  expect(StubWorker.terminated).toBe(1);
});

test("counts whitespace-only runs toward the limit, so a page of them is stopped and refused too", async () => {
  const third = " ".repeat(3_000_000);
  const source = streamed([[run(third, 700)], [run(third, 688)], [run(third, 676)], [run("never pulled", 664)]]);
  const page = { streamTextContent: vi.fn(() => source.stream), cleanup: vi.fn() };
  const { task, worker } = opened(page, 1);

  const read = await readPdf(new File(["%PDF-1.7"], "a.pdf"));

  expect(source.pulled()).toBe(3);
  expect(read).toEqual({ ok: false, why: "too-big" });
  expect(task.destroy).toHaveBeenCalledOnce();
  expect(worker.destroy).toHaveBeenCalledOnce();
  expect(StubWorker.terminated).toBe(1);
});

test("breaks lines where pdf.js saw them end, and paragraphs at a wider gap", () => {
  const items = [
    run("The Lighthouse", 720, true, 18),
    run("The keeper climbed", 690, false),
    run(" ", 690, false),
    run("the stairs", 690),
    run("every evening.", 676),
    run("He counted the steps.", 656),
    run("Ships passed.", 642),
  ];

  const page = foldPage();
  page.add(items);

  expect(page.text()).toBe("The Lighthouse\n\nThe keeper climbed the stairs\nevery evening.\n\nHe counted the steps.\nShips passed.");
});

test("reads a double-spaced page as one paragraph, not a paragraph a line", () => {
  const page = foldPage();
  page.add([run("One line", 700), run("wraps onto", 672), run("the next.", 644)]);

  expect(page.text()).toBe("One line\nwraps onto\nthe next.");
});

test("skips marked-content markers and ends a line at an empty end-of-line run", () => {
  const items = [
    { type: "beginMarkedContent", id: "" },
    run("First", 700, false),
    run("", 700, true),
    { type: "endMarkedContent", id: "" },
    run("second", 686),
  ];

  const page = foldPage();
  page.add(items);

  expect(page.text()).toBe("First\nsecond");
});

test("joins a line that pdf.js split across two chunks", () => {
  const page = foldPage();
  page.add([run("The keeper", 700, false)]);
  page.add([run(" climbed", 700), run("the stairs.", 686)]);

  expect(page.text()).toBe("The keeper climbed\nthe stairs.");
});
