import { strToU8, zipSync } from "fflate";
import { describe, expect, test, vi } from "vitest";
import { INFLATE_CAP } from "./formats/unzip";
import { readSourceFile, readSourceLink, tidyMarkdown } from "./importSource";

// jsdom has no Worker, so the unzip runs in-thread.
vi.mock("./formats/unzip", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./formats/unzip")>();
  return { ...actual, unzip: async (bytes: Uint8Array, want: import("./formats/unzip").Want) => actual.unzipEntries(bytes, want) };
});

// pdf.js needs a real browser; its reader is exercised there, and here only
// what the composer says about the text it returns.
vi.mock("./formats/pdf", () => ({ readPdf: async () => ({ ok: true, text: " \n " }) }));

const file = (text: string, name: string, type = "") => new File([text], name, { type });

const docx = (paragraphs: string[] | Uint8Array, name = "report.docx") =>
  new File(
    [
      zipSync({
        "word/document.xml": Array.isArray(paragraphs)
          ? strToU8(
              `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>${paragraphs
                .map((words) => `<w:p><w:r><w:t>${words}</w:t></w:r></w:p>`)
                .join("")}</w:body></w:document>`,
            )
          : paragraphs,
      }),
    ],
    name,
  );

// A one-chapter EPUB whose chapter is listed as encrypted, the way a
// DRM-locked book is.
const lockedEpub = () =>
  new File(
    [
      zipSync({
        "META-INF/container.xml": strToU8(
          `<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>`,
        ),
        "META-INF/encryption.xml": strToU8(
          `<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:CipherData><enc:CipherReference URI="one.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>`,
        ),
        "book.opf": strToU8(
          `<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="one"/></spine></package>`,
        ),
        "one.xhtml": new Uint8Array([0x8f, 0x13, 0xfe]),
      }),
    ],
    "locked.epub",
  );

describe("readSourceFile", () => {
  test("refuses a file over the byte cap without reading it", async () => {
    const big = file("", "book.txt", "text/plain");
    Object.defineProperty(big, "size", { value: 9 * 1024 * 1024 });
    Object.defineProperty(big, "text", {
      value: () => Promise.reject(new Error("read a file it should have refused")),
    });
    expect(await readSourceFile(big)).toEqual({
      ok: false,
      reason: "book.txt is 9.0 MB, more than the 8.0 MB Readily opens.",
    });
  });

  test("refuses a file of a format it does not read", async () => {
    expect(await readSourceFile(file("GIF89a", "photo.gif", "image/gif"))).toEqual({
      ok: false,
      reason: "Readily opens .txt, .md, .docx, .epub and .pdf files, and photo.gif is none of these.",
    });
  });

  test("refuses a DRM-locked book with a sentence that says so", async () => {
    expect(await readSourceFile(lockedEpub())).toEqual({
      ok: false,
      reason: "locked.epub is locked with a password or DRM, so Readily can't read it.",
    });
  });

  test("says why a PDF with no text in it may have none", async () => {
    expect(await readSourceFile(file("%PDF-1.7", "scan.pdf"))).toEqual({
      ok: false,
      reason: "scan.pdf has no text in it. It may be a scan, which holds pictures of pages rather than text.",
    });
  });

  test("reads a Word document into paragraphs", async () => {
    expect(await readSourceFile(docx(["A heading", "A paragraph."]))).toEqual({
      ok: true,
      text: "A heading\n\nA paragraph.",
    });
  });

  test("refuses a document that unpacks past the cap", async () => {
    expect(await readSourceFile(docx(new Uint8Array(INFLATE_CAP + 1), "bomb.docx"))).toEqual({
      ok: false,
      reason: "bomb.docx unpacks to more than Readily opens.",
    });
  });

  test("refuses a document that is not what its name says", async () => {
    expect(await readSourceFile(file("just some text", "fake.docx"))).toEqual({
      ok: false,
      reason: "Readily couldn't read fake.docx.",
    });
  });

  test("refuses a document with more text than a file of text may hold", async () => {
    expect(await readSourceFile(docx(["a".repeat(8 * 1024 * 1024 + 1)], "long.docx"))).toEqual({
      ok: false,
      reason: "long.docx holds more text than Readily opens at once.",
    });
  });

  test("reads a .txt file as it is, with its line endings made \\n", async () => {
    expect(await readSourceFile(file("# Not a heading\r\nSecond *line*.", "notes.txt"))).toEqual({
      ok: true,
      text: "# Not a heading\nSecond *line*.",
    });
  });

  test("tidies a .md file before it becomes the Source", async () => {
    expect(await readSourceFile(file("# Title\n\nSome **bold** words.", "notes.md"))).toEqual({
      ok: true,
      text: "Title\n\nSome bold words.",
    });
  });

  test("accepts a Markdown file by its type when the name says nothing", async () => {
    expect(await readSourceFile(file("- item", "README", "text/markdown"))).toEqual({
      ok: true,
      text: "item",
    });
  });

  test("refuses a file with no text in it", async () => {
    expect(await readSourceFile(file("---\n\n", "blank.md"))).toEqual({
      ok: false,
      reason: "blank.md has no text in it.",
    });
  });
});

describe("readSourceLink", () => {
  const page = (text: string, contentType = "text/plain") => ({
    bytes: new TextEncoder().encode(text),
    contentType,
  });

  test("reads the page the Engine fetched into the Source", async () => {
    const fetchPage = vi.fn(async () => page("A page's words."));
    expect(await readSourceLink("https://example.com/story", fetchPage)).toEqual({
      ok: true,
      text: "A page's words.",
    });
    expect(fetchPage).toHaveBeenCalledWith("https://example.com/story");
  });

  test.each([
    ["  example.com/story  ", "https://example.com/story"],
    ["example.com:8443/article", "https://example.com:8443/article"],
    ["example.com:8443", "https://example.com:8443/"],
    ["localhost:3000/x", "https://localhost:3000/x"],
    ["[::1]:8080/x", "https://[::1]:8080/x"],
  ])("reads %j typed without its scheme as https, the way a browser would", async (typed, href) => {
    const fetchPage = vi.fn(async () => page("Words."));
    await readSourceLink(typed, fetchPage);
    expect(fetchPage).toHaveBeenCalledWith(href);
  });

  test.each(["http://example.com/", "file:///etc/hosts", "javascript:alert(1)", "mailto:someone@example.com"])(
    "hands %j to the Engine under the scheme it was typed with",
    async (typed) => {
      const fetchPage = vi.fn(async () => page("Words."));
      await readSourceLink(typed, fetchPage);
      expect(fetchPage).toHaveBeenCalledWith(typed);
    },
  );

  test("a bare IPv6 address is not a link, as it would not be in a browser", async () => {
    const fetchPage = vi.fn(async () => page("Words."));
    expect(await readSourceLink("::1/x", fetchPage)).toEqual({
      ok: false,
      reason: "“::1/x” is not a link Readily can open.",
    });
    expect(fetchPage).not.toHaveBeenCalled();
  });

  test("refuses what is not a link without asking the Engine", async () => {
    const fetchPage = vi.fn(async () => page("Words."));
    expect(await readSourceLink("not a link at all", fetchPage)).toEqual({
      ok: false,
      reason: "“not a link at all” is not a link Readily can open.",
    });
    expect(fetchPage).not.toHaveBeenCalled();
  });

  test("says what the Engine said when it would not fetch the page", async () => {
    const refused = async () => {
      throw new Error("Only public https pages can be read.");
    };
    expect(await readSourceLink("http://example.com", refused)).toEqual({
      ok: false,
      reason: "Only public https pages can be read.",
    });
  });

  test("refuses a page built of more elements than Readily reads, by its host", async () => {
    const html = `<html><body><article>${"<b>x</b>".repeat(50_001)}</article></body></html>`;
    expect(await readSourceLink("https://example.com/", async () => page(html, "text/html"))).toEqual({
      ok: false,
      reason: "The page at example.com is larger than Readily opens.",
    });
  });

  test("refuses a page with no text in it, by its host", async () => {
    expect(await readSourceLink("https://example.com/", async () => page("<p> </p>", "text/html"))).toEqual({
      ok: false,
      reason: "example.com has no text in it.",
    });
  });
});

describe("tidyMarkdown", () => {
  test("drops ATX heading markers, closing hashes included", () => {
    expect(tidyMarkdown("# One\n### Three ###\n#hashtag")).toBe("One\nThree\n#hashtag");
  });

  test("drops fence lines and keeps what they fence as written", () => {
    expect(tidyMarkdown("```ts\nconst a = *b*;\n```\n~~~\n# kept\n~~~")).toBe(
      "const a = *b*;\n# kept",
    );
  });

  test("ends a fenced block only at a fence of its own kind", () => {
    expect(tidyMarkdown("```\n~~~\n````x\n**bold**\n```\n**after**")).toBe(
      "~~~\n````x\n**bold**\nafter",
    );
    expect(tidyMarkdown("```\n    ```\n**in**\n```\n**after**")).toBe("    ```\n**in**\nafter");
    expect(tidyMarkdown("- item\n    ```\n    **code**\n    ```\n**after**")).toBe(
      "item\n    **code**\nafter",
    );
  });

  test("drops bullets and keeps an ordered list's numbers", () => {
    expect(tidyMarkdown("- dash\n* star\n+ plus\n1. one\n2024. A year like no other.")).toBe(
      "dash\nstar\nplus\n1. one\n2024. A year like no other.",
    );
  });

  test("drops blockquote markers, nested ones too", () => {
    expect(tidyMarkdown("> quoted\n>> deeper\n> # heading")).toBe("quoted\ndeeper\nheading");
  });

  test("drops emphasis markers around words", () => {
    expect(
      tidyMarkdown("*it* _it_ **bold** __bold__ ~~gone~~"),
    ).toBe("it it bold bold gone");
  });

  test("leaves asterisks and underscores that are not emphasis", () => {
    expect(tidyMarkdown("2 * 3 * 4 and snake_case_name")).toBe("2 * 3 * 4 and snake_case_name");
  });

  test("reduces links and images to their text and alt", () => {
    expect(tidyMarkdown("See [the docs](https://example.com) and ![a cat](cat.png).")).toBe(
      "See the docs and a cat.",
    );
  });

  test("reduces a link whose URL holds parentheses to its text", () => {
    expect(tidyMarkdown("A [wiki](https://en.wikipedia.org/wiki/Foo_(bar)) page.")).toBe(
      "A wiki page.",
    );
  });

  test("drops setext underlines", () => {
    expect(tidyMarkdown("Title\n=====\nSubtitle\n--")).toBe("Title\nSubtitle");
  });

  test("drops horizontal rules", () => {
    expect(tidyMarkdown("Before\n\n***\n\n- - -\n___\nAfter")).toBe("Before\n\n\nAfter");
  });

  test("keeps a malformed file's cost linear in its length", () => {
    // Openers that never close, and a heading line that cannot match `.`,
    // each took tens of seconds or more before every scan was bounded.
    for (const malformed of ["[".repeat(200_000), " **a".repeat(100_000), `#${" ".repeat(100_000)}\u2028`]) {
      const started = performance.now();
      tidyMarkdown(malformed);
      expect(performance.now() - started).toBeLessThan(2000);
    }
  });

  test("leaves ordinary prose unchanged", () => {
    const prose =
      "It was the best of times, it was the worst of times.\n\nShe said: \"Don't - please.\" (2024)";
    expect(tidyMarkdown(prose)).toBe(prose);
  });
});
