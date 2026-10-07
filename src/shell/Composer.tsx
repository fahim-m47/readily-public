import { useRef, useState, type DragEvent, type ReactNode } from "react";
import { FileText, Link, Play } from "lucide-react";
import type { EngineClient } from "../engine/client";
import { estimateSource } from "./estimate";
import {
  readSourceFile,
  readSourceLink,
  SOURCE_FILE_ACCEPT,
  SOURCE_FILE_KINDS,
  type ImportResult,
} from "./importSource";

export type ComposerProps = {
  text: string;
  onTextChange: (text: string) => void;
  // Whether the Engine can take a Narration right now — up, and holding a
  // Voice Model to read with. A Source that is blank or past the Engine's
  // limit is refused here regardless.
  canNarrate: boolean;
  onNarrate: () => void;
  // The Engine's page fetch, called only when the reader opens a link.
  fetchPage: EngineClient["fetchPage"];
  // The Voice Model pill.
  model: ReactNode;
  // The composer's one line about the Voice: why a reader cannot narrate
  // yet, or what the Engine said about the last Voice they chose when it
  // refused. Nothing to say most of the time.
  notice: string | null;
};

// Whether a drag is carrying files from the Finder, as opposed to text
// dragged within the page, which the textarea handles on its own.
const carriesFiles = (event: DragEvent) => event.dataTransfer.types.includes("Files");

// The one card in the centre column: paste, type or drop text, see what it
// costs, press Narrate.
//
// The card decides for itself whether the Source is narratable, because the
// two reasons it is not — no text, or more text than one Narration takes —
// are both facts about the text in this textarea and both need saying in the
// same line the estimate uses.
//
// A dropped or opened file, or a page opened with "Open link", replaces the
// draft through `onTextChange`, the same path typing takes, so to the rest of
// the shell an imported Source is one the reader pasted. The drop reaches this
// card only because Tauri's own drag-and-drop handler is off for the window
// (`dragDropEnabled` in `tauri.conf.json`); the file arrives as a browser
// `File`, never a path (threat model, B4). A link is fetched only when the
// reader presses Read; one pasted into the draft stays text.
export default function Composer({
  text,
  onTextChange,
  canNarrate,
  onNarrate,
  fetchPage,
  model,
  notice,
}: ComposerProps) {
  const estimate = estimateSource(text);
  const ready = canNarrate && estimate.narratable;
  const [dropping, setDropping] = useState(false);
  // What the last file or link is doing to the draft: being opened, or why
  // it could not become the Source. Held here rather than in App's
  // `notice`, because it is about this card's draft and is cleared by this
  // card's own typing; it shares the notice line and takes it over while it
  // has something to say.
  const [importNotice, setImportNotice] = useState<string | null>(null);
  // Bumped by every edit and every import, so a read that resolves after
  // newer typing or a newer drop knows it has been overtaken and drops its
  // text instead of replacing the reader's.
  const edits = useRef(0);
  // How many elements inside the card a file drag is over, counted by
  // dragenter and dragleave. The card is highlighted while it is above zero.
  // `relatedTarget` would say whether a dragleave went to a child, but
  // WKWebView on macOS 14 and 15 leaves it null (WebKit bug 66547), so
  // crossing from the padding onto the textarea would flicker the highlight.
  const dragDepth = useRef(0);
  const picker = useRef<HTMLInputElement>(null);
  // The link being typed into the "Open link" row, or null while it is shut.
  const [link, setLink] = useState<string | null>(null);
  const linkButton = useRef<HTMLButtonElement>(null);

  const edit = (next: string) => {
    edits.current += 1;
    setImportNotice(null);
    onTextChange(next);
  };

  // One file at a time: a second file or a folder is refused with the draft
  // left as it was.
  const importFiles = (files: File[], folder: boolean) => {
    if (folder || files.length !== 1) {
      edits.current += 1;
      setImportNotice(`Readily opens one ${SOURCE_FILE_KINDS} file at a time, not a folder or several files.`);
      return;
    }
    void adopt(`Opening ${files[0].name}…`, readSourceFile(files[0]));
  };

  // Makes what a file or page read into the draft, saying `opening` while
  // it reads, since a long book or a slow site takes a moment. Typing or
  // another import before it finishes overtakes it and its text is dropped;
  // otherwise the line says why it could not be read. Resolves to whether
  // the draft took it.
  const adopt = async (opening: string, reading: Promise<ImportResult>) => {
    edits.current += 1;
    const asked = edits.current;
    setImportNotice(opening);
    const result = await reading;
    if (edits.current !== asked) return false;
    setImportNotice(result.ok ? null : result.reason);
    if (result.ok) onTextChange(result.text);
    return result.ok;
  };

  // Shuts the "Open link" row and hands focus back to the button that opened it.
  const closeLink = () => {
    setLink(null);
    linkButton.current?.focus();
  };

  // The row stays open with the link in it when the page cannot be read, so
  // the reader can correct it.
  const openLink = (typed: string) => {
    void adopt("Opening the page…", readSourceLink(typed, fetchPage)).then((adopted) => {
      if (adopted) closeLink();
    });
  };

  const said = importNotice ?? notice;

  return (
    <div
      className={`composer${dropping ? " composer--dropping" : ""}`}
      onDragEnter={(event) => {
        if (!carriesFiles(event)) return;
        dragDepth.current += 1;
        setDropping(true);
      }}
      onDragOver={(event) => {
        if (!carriesFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(event) => {
        if (!carriesFiles(event)) return;
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDropping(false);
      }}
      onDrop={(event) => {
        if (!carriesFiles(event)) return;
        event.preventDefault();
        dragDepth.current = 0;
        setDropping(false);
        const folder = [...(event.dataTransfer.items ?? [])].some(
          (item) => item.webkitGetAsEntry()?.isDirectory,
        );
        importFiles([...event.dataTransfer.files], folder);
      }}
    >
      <label className="visually-hidden" htmlFor="source">
        Source
      </label>
      <textarea
        id="source"
        autoFocus
        aria-describedby="source-estimate"
        className="composer__source"
        value={text}
        placeholder={`Paste, type, or drop a ${SOURCE_FILE_KINDS} file…`}
        onChange={(event) => edit(event.target.value)}
      />

      {link !== null && (
        <form
          className="composer__link"
          onSubmit={(event) => {
            event.preventDefault();
            openLink(link);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") closeLink();
          }}
        >
          <input
            aria-label="Link"
            className="composer__link-input"
            type="text"
            inputMode="url"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            autoFocus
            placeholder="https://…"
            value={link}
            onChange={(event) => setLink(event.target.value)}
          />
          <button className="composer__link-read" type="submit" disabled={link.trim() === ""}>
            Read
          </button>
        </form>
      )}

      <div className="composer__foot">
        <span
          id="source-estimate"
          className={`composer__estimate${estimate.overBy > 0 ? " composer__estimate--over" : ""}`}
        >
          {estimate.summary}
        </span>
        <input
          ref={picker}
          type="file"
          accept={SOURCE_FILE_ACCEPT}
          hidden
          onChange={(event) => {
            importFiles([...(event.target.files ?? [])], false);
            // Cleared so choosing the same file again is still a change.
            event.target.value = "";
          }}
        />
        <button
          aria-label="Open file"
          className="composer__open"
          type="button"
          onClick={() => picker.current?.click()}
        >
          <FileText size={16} />
        </button>
        <button
          ref={linkButton}
          aria-label="Open link"
          aria-expanded={link !== null}
          className="composer__open"
          type="button"
          onClick={() => (link === null ? setLink("") : closeLink())}
        >
          <Link size={16} />
        </button>
        {model}
        <button
          aria-label="Narrate"
          className={`composer__narrate${ready ? " composer__narrate--ready" : ""}`}
          type="button"
          disabled={!ready}
          onClick={onNarrate}
        >
          <Play size={16} fill="currentColor" strokeWidth={0} />
        </button>
      </div>

      {/* Mounted even when silent, so a refused switch is an update rather
          than an arrival — the same reason the sheet's notice is. */}
      <p
        className={`composer__notice${said ? "" : " composer__notice--quiet"}`}
        aria-live="polite"
      >
        {said ?? ""}
      </p>
    </div>
  );
}
