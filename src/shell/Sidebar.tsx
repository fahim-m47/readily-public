import type { ReactNode } from "react";
import { PanelLeft, Plus } from "lucide-react";
import { useEffect, useRef } from "react";
import Wordmark from "./Wordmark";
import type { ShellStatus } from "./lifecycle";

export type SidebarProps = {
  modeControl?: ReactNode;
  // History's rows, once there are any. The sidebar owns the chrome around
  // them — the heading, the scroll region, and the fresh-install line that
  // stands in when this is empty — so History itself is only rows.
  history?: ReactNode;
  // Returns to the composer, draft and all. A Narration already on the
  // Engine keeps running — stopping one is the Stop button's job.
  onNewNarration: () => void;
  // Opens Settings: the two retention knobs, what Readily is using the disk
  // for, and the way to the data folder.
  onSettings: () => void;
  // Whether the sidebar is on screen. It stays mounted while hidden, so
  // History keeps its scroll and its armed delete, and its live regions
  // keep their messages rather than arriving already holding them.
  open: boolean;
  // Collapses the sidebar. The button that brings it back sits in the main
  // column's strip, in the same spot beside the traffic lights.
  onHide: () => void;
  status: ShellStatus;
};

// The sidebar toggle, either side of the collapse: one button, in the title
// bar, beside the traffic lights. Each press hides the button that was
// pressed, so the one that takes its place takes the focus too: the main
// strip's copy mounts only on a press and can simply `autoFocus`, the
// sidebar's is always mounted and is focused by `focusWhen` instead.
export function SidebarToggle({
  open,
  onClick,
  autoFocus = false,
  focusWhen,
}: {
  open: boolean;
  onClick: () => void;
  autoFocus?: boolean;
  focusWhen?: boolean;
}) {
  const ref = useRef<HTMLButtonElement>(null);
  const wasFocusing = useRef(focusWhen);
  useEffect(() => {
    if (focusWhen && !wasFocusing.current) ref.current?.focus();
    wasFocusing.current = focusWhen;
  }, [focusWhen]);
  return (
    <button
      ref={ref}
      aria-controls="sidebar"
      aria-expanded={open}
      aria-label={open ? "Hide sidebar" : "Show sidebar"}
      autoFocus={autoFocus}
      className="bar__button"
      onClick={onClick}
      type="button"
    >
      <PanelLeft aria-hidden="true" size={18} strokeWidth={1.75} />
    </button>
  );
}

// The shell's persistent left rail: always there, never a takeover surface.
//
// It holds three fixed things — the wordmark and New Narration at the top,
// History in the middle, the Engine's state and the way into Settings at the
// foot — and grows only by what is handed to it, so a feature that adds to the
// sidebar adds rows rather than restructuring it.
export default function Sidebar({
  modeControl,
  history,
  onNewNarration,
  onSettings,
  open,
  onHide,
  status,
}: SidebarProps) {
  return (
    <aside className="side" id="sidebar" aria-label="Sidebar" hidden={!open}>
      {/* The title bar's left half: the traffic lights sit in its padding. */}
      <div className="bar bar--lights side__bar" data-tauri-drag-region="deep">
        <SidebarToggle open focusWhen={open} onClick={onHide} />
      </div>
      <div className="side__top">
        <span className="side__wordmark">
          <Wordmark />
        </span>
        {modeControl}
        <button
          className="side__new"
          type="button"
          onClick={onNewNarration}
          aria-label="New Narration"
        >
          <Plus size={16} />
        </button>
      </div>

      <h2 className="side__heading" id="history-heading">
        History
      </h2>
      {/* No `tabIndex` here, deliberately: History's rows are buttons, and a
          scroll region whose children are focusable is already scrollable
          from the keyboard. Empty, it holds nothing at all — so a tab stop
          here would be a stop on nothing. */}
      <div className="side__history" aria-labelledby="history-heading" role="group">
        {history}
      </div>

      <button className="side__settings" type="button" onClick={onSettings}>
        Settings
        {status.tone === "ready" && (
          <span aria-hidden="true" className="side__dot side__dot--ready" />
        )}
      </button>

      <p
        className={`side__foot${status.tone === "ready" ? " visually-hidden" : ""}`}
        aria-live="polite"
      >
        {status.tone !== "ready" && (
          <span
            aria-hidden="true"
            className={`side__dot side__dot--${status.tone}`}
          />
        )}
        {status.message}
      </p>
    </aside>
  );
}
