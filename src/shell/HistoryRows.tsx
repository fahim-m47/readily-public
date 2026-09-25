import { useState } from "react";
import type { HistoryEntry } from "../engine/client";
import { Download, Trash2, CircleAlert } from "lucide-react";
import { describeRow } from "./history";

export type HistoryRowsProps = {
  // Newest first, in the Engine's order. History is never re-sorted here.
  entries: HistoryEntry[];
  // The Narration the Engine is playing right now, when it is one of these.
  activeId: string | null;
  // The last thing History did that the reader is owed a word about — a
  // request the Engine refused.
  notice: string | null;
  onOpen: (narrationId: string) => void;
  onDelete: (narrationId: string) => void;
  onExport: (entry: HistoryEntry) => void;
};

// History's rows, and nothing around them — the sidebar owns the heading and
// the scroll region (`Sidebar.tsx`).
//
// Each row is one button, which is what makes the scroll region reachable
// from the keyboard, and each row admits what it no longer has: audio
// retention swept away, and Blocks that could not be read. Selecting a row
// opens it paused; the marker clears when the Engine has re-made what was missing.
export default function HistoryRows({
  entries,
  activeId,
  notice,
  onOpen,
  onDelete,
  onExport,
}: HistoryRowsProps) {
  // Delete is permanent — an entry is the one thing Readily promises to keep
  // — so the button arms before it fires. Arming the same button rather than
  // swapping in a confirm pair keeps the reader's focus where they put it,
  // and moving off the button disarms it.
  const [armed, setArmed] = useState<string | null>(null);

  return (
    <>
      {/* Mounted even when silent: a live region that arrives already
          holding its message is announced unreliably. */}
      <p
        className={`history__notice${notice ? "" : " history__notice--quiet"}`}
        aria-live="polite"
      >
        {notice ?? ""}
      </p>

      <ul className="history">
        {entries.map((entry) => {
          const row = describeRow(entry);
          const isArmed = armed === entry.id;

          return (
            <li className="history__row" key={entry.id}>
              <button
                className="history__open"
                type="button"
                aria-current={entry.id === activeId ? "true" : undefined}
                onClick={() => onOpen(entry.id)}
              >
                <span className="history__title">{row.title}</span>
                <span className="history__meta">
                  <span className="visually-hidden">{row.meta}</span>
                  {row.markers.map((marker) => (
                    <span
                      className={marker.kind === "evicted" ? "visually-hidden" : `history__marker history__marker--${marker.kind}`}
                      key={marker.kind}
                      title={marker.label}
                    >
                      {marker.kind !== "evicted" && <CircleAlert size={14} />}
                      <span className="visually-hidden">{marker.label}</span>
                    </span>
                  ))}
                </span>
              </button>

              <button
                className="history__export"
                type="button"
                aria-label={`Export ${row.title}`}
                title="Export audio"
                onClick={() => onExport(entry)}
              >
                <Download size={14} />
              </button>

              <button
                className={`history__delete${isArmed ? " history__delete--armed" : ""}`}
                type="button"
                aria-label={
                  isArmed
                    ? `Delete ${row.title} permanently`
                    : `Delete ${row.title}`
                }
                onClick={() => {
                  setArmed(null);
                  if (isArmed) onDelete(entry.id);
                  else setArmed(entry.id);
                }}
                onBlur={() => setArmed(null)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") setArmed(null);
                }}
              >
                {isArmed ? "Delete?" : <Trash2 size={14} />}
              </button>
            </li>
          );
        })}
      </ul>
    </>
  );
}
