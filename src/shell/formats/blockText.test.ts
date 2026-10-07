import { expect, test } from "vitest";
import { blockText, tidyText } from "./blockText";

const html = (body: string) => new DOMParser().parseFromString(body, "text/html").documentElement;

test("gives each block element a paragraph and runs inline elements together", () => {
  const root = html("<h1>The Lighthouse</h1><p>The keeper <em>climbed</em>\n    the stairs.</p><p>One<br>two</p>");

  expect(blockText(root)).toBe("The Lighthouse\n\nThe keeper climbed the stairs.\n\nOne\ntwo");
});

test("leaves out the head, scripts, styles and navigation", () => {
  const root = html(
    "<head><title>Title</title><style>p {}</style></head>" +
      "<body><nav><a href='#'>Contents</a></nav><p>Text</p><script>run()</script></body>",
  );

  expect(blockText(root)).toBe("Text");
});

test("leaves out a hidden element, unless it is hidden only until found", () => {
  expect(blockText(html("<p>Shown</p><aside hidden>draft note</aside><p hidden='until-found'>Found later</p>"))).toBe(
    "Shown\n\nFound later",
  );
});

test("keeps the line breaks inside preformatted text", () => {
  expect(blockText(html("<pre>first line\nsecond line</pre>"))).toBe("first line\nsecond line");
});

test("separates table cells", () => {
  expect(blockText(html("<table><tr><th>Tide</th><td>High</td></tr></table>"))).toBe("Tide\n\nHigh");
});

test("numbers the items of an ordered list the way a numbered Markdown list reads", () => {
  expect(blockText(html("<ol><li>First</li><li>Second</li></ol><ul><li>Bullet</li></ul>"))).toBe(
    "1. First\n\n2. Second\n\nBullet",
  );
});

test("numbers a list in the letters or roman numerals its type asks for, and an item in its own", () => {
  const list =
    "<ol type='A'><li>Alpha<ol type='i'><li>One</li><li>Two</li><li>Three</li><li>Four</li></ol></li><li>Beta</li><li type='1'>Gamma</li></ol>";

  expect(blockText(html(list))).toBe("A. Alpha\n\ni. One\n\nii. Two\n\niii. Three\n\niv. Four\n\nB. Beta\n\n3. Gamma");
});

test("counts from an ordered list's start and an item's own value", () => {
  expect(blockText(html("<ol start='4'><li>Four</li><li value='9'>Nine</li><li>Ten</li></ol>"))).toBe(
    "4. Four\n\n9. Nine\n\n10. Ten",
  );
});

test("counts a reversed list down from its length, and on down from an item's own value", () => {
  expect(blockText(html("<ol reversed><li>Three</li><li value='9'>Nine</li><li>Eight</li></ol>"))).toBe(
    "3. Three\n\n9. Nine\n\n8. Eight",
  );
});

test("keeps an item's number on the line of its first paragraph", () => {
  expect(blockText(html("<ol><li>\n  <p>First</p><p>More</p></li></ol>"))).toBe("1. First\n\nMore");
});

test("keeps an item's number past an empty element, and drops an empty item's number", () => {
  expect(blockText(html("<ol><li><span class='icon'></span>First</li><li></li><li>Third</li></ol>"))).toBe(
    "1. First\n\n3. Third",
  );
});

test("lets an inner item take over from an outer item that had no words yet", () => {
  expect(blockText(html("<ol><li><ol><li>x</li></ol>Tail</li><li>Second</li></ol>"))).toBe(
    "1. x\n\nTail\n\n2. Second",
  );
});

test("says an image's alt text where the image sits and nothing for a decorative one", () => {
  expect(
    blockText(html("<p>See <img alt='a map of the route' src='map.png'> here.</p><p>Or<img alt=' '><img alt=''><img>.</p>")),
  ).toBe("See a map of the route here.\n\nOr.");
});

test("trims each line's ends, collapses blank runs and drops the outer whitespace", () => {
  expect(tidyText(" \t one \r\n\n\n\n  two　\nthree  \n\n")).toBe("one\n\ntwo\nthree");
});

test("settles a long run of spaces without a newline in linear time", () => {
  const text = `a${" ".repeat(50_000)}b`;
  const started = performance.now();

  expect(tidyText(text)).toBe(text);
  expect(performance.now() - started).toBeLessThan(250);
});
