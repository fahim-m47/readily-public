import { describe, expect, test } from "vitest";
import { PAGE_ELEMENT_LIMIT, readPage } from "./page";

const utf8 = (text: string) => new TextEncoder().encode(text);
// The text of a page that must read.
const read = (bytes: Uint8Array, contentType: string) => {
  const page = readPage(bytes, contentType);
  if (!page.ok) throw new Error(page.why);
  return page.text;
};
// Windows-1252 bytes for text that is ASCII apart from the characters given
// as their single byte, the way a legacy page arrives.
const cp1252 = (text: string) => Uint8Array.from(text, (char) => char.charCodeAt(0));

const paragraph = (n: number) =>
  `<p>Paragraph ${n} of the story, long enough to read as prose rather than as a menu, ` +
  `with a comma or two, a clause that runs on, and a full stop at the end of it.</p>`;

const article = (body: string, head = "") => `<!doctype html>
<html><head><title>The Lighthouse Keeper</title>${head}</head>
<body>
  <nav><a href="/">Home</a> <a href="/world">World</a> <a href="/sport">Sport</a></nav>
  <script>document.title = "ran";</script>
  <article>
    <h1>The Lighthouse Keeper</h1>
    ${body}
  </article>
  <footer>Copyright Example News. All rights reserved.</footer>
</body></html>`;

describe("readPage", () => {
  test("an article page reads as its title, then its paragraphs, without the page around it", () => {
    const text = read(utf8(article([1, 2, 3, 4].map(paragraph).join(""))), "text/html");

    expect(text.startsWith("The Lighthouse Keeper\n\nParagraph 1 of the story")).toBe(true);
    expect(text.split("\n\n")).toHaveLength(5);
    expect(text).not.toMatch(/Home|World|Copyright|ran/);
  });

  test("a title the article already opens with is not said twice", () => {
    const text = read(utf8(article([1, 2, 3, 4].map(paragraph).join(""))), "text/html");
    expect(text.match(/The Lighthouse Keeper/g)).toHaveLength(1);
  });

  test("a plain-text page is its text, with its line endings settled the way a .txt file's are", () => {
    expect(read(utf8("First line.\r\nSecond line.\r\n"), "text/plain")).toBe("First line.\nSecond line.\n");
  });

  test("the charset the page's header names decodes it", () => {
    expect(read(cp1252("Caf\xe9 cr\xe8me"), "text/plain; charset=windows-1252")).toBe("Café crème");
  });

  test("an HTML page's own meta charset decodes it when the header names none", () => {
    const body = [1, 2, 3].map(paragraph).join("") + "<p>Caf\xe9 cr\xe8me, and a na\xefve r\xe9sum\xe9.</p>";
    const text = read(cp1252(article(body, '<meta charset="windows-1252">')), "text/html");
    expect(text).toContain("Café crème, and a naïve résumé.");
  });

  test("the older http-equiv meta names a charset too", () => {
    const meta = '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">';
    const body = [1, 2, 3].map(paragraph).join("") + "<p>Caf\xe9.</p>";
    expect(read(cp1252(article(body, meta)), "text/html")).toContain("Café.");
  });

  test("a page that names no charset reads as UTF-8", () => {
    expect(read(utf8("Café, naïve, 東京."), "text/plain")).toBe("Café, naïve, 東京.");
  });

  test("a charset no decoder knows reads as UTF-8 rather than failing", () => {
    expect(read(utf8("Café."), "text/plain; charset=klingon")).toBe("Café.");
  });

  test("a page with nothing to read is empty", () => {
    expect(read(utf8("<!doctype html><html><body><nav>Home</nav></body></html>"), "text/html")).toBe("");
  });

  test("a page built of more elements than Readily reads is refused before it is walked", () => {
    const pieces = "<b>x</b>".repeat(PAGE_ELEMENT_LIMIT);
    expect(readPage(utf8(article(pieces)), "text/html")).toEqual({ ok: false, why: "too-big" });
  });
});
