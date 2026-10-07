import type { Extracted } from "../importSource";
import { parseXml, tidyText } from "./blockText";
import { unzip } from "./unzip";

const DOCUMENT = "word/document.xml";
// Transitional and Strict OOXML name the same elements in two namespaces.
const WORD = new Set([
  "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
  "http://purl.oclc.org/ooxml/wordprocessingml/main",
]);
const COMPATIBILITY = "http://schemas.openxmlformats.org/markup-compatibility/2006";

// Walks the body in document order. Only the main document part is ever
// unzipped, so headers, footers, footnotes and comments are never read.
// Property elements (`w:pPr`, `w:rPr`, …) hold formatting, and a `w:tab`
// inside one is a tab stop rather than a tab, so they are skipped whole.
// A text box is written twice, once for current Word and once as a legacy
// fallback, so the fallback is skipped too.
const walk = (parent: Element, out: string[]) => {
  for (const node of parent.children) {
    if (node.namespaceURI === COMPATIBILITY && node.localName === "Fallback") continue;
    if (!WORD.has(node.namespaceURI ?? "")) {
      walk(node, out);
      continue;
    }
    const name = node.localName;
    if (name.endsWith("Pr") || name === "del" || name === "moveFrom") continue;
    if (name === "t") out.push(node.textContent ?? "");
    else if (name === "tab") out.push("\t");
    else if (name === "br" || name === "cr") out.push("\n");
    else {
      walk(node, out);
      if (name === "p") out.push("\n\n");
    }
  }
};

// The text of a Word document: its paragraphs, tables and text boxes, with
// tracked insertions kept and tracked deletions left out.
export const readDocx = async (file: File): Promise<Extracted> => {
  const unzipped = await unzip(new Uint8Array(await file.arrayBuffer()), { names: [DOCUMENT] });
  if (!unzipped.ok) return unzipped;
  const xml = unzipped.files.get(DOCUMENT);
  const document = xml === undefined ? null : parseXml(xml);
  if (!document) return { ok: false, why: "damaged" };
  const out: string[] = [];
  walk(document.documentElement, out);
  return { ok: true, text: tidyText(out.join("")) };
};
