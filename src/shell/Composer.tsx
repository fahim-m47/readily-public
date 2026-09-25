import type { ReactNode } from "react";
import { Play } from "lucide-react";
import { estimateSource } from "./estimate";

export type ComposerProps = {
  text: string;
  onTextChange: (text: string) => void;
  // Whether the Engine can take a Narration right now — up, and holding a
  // Voice Model to read with. A Source that is blank or past the Engine's
  // limit is refused here regardless.
  canNarrate: boolean;
  onNarrate: () => void;
  // The Voice Model pill.
  model: ReactNode;
  // The composer's one line about the Voice: why a reader cannot narrate
  // yet, or what the Engine said about the last Voice they chose when it
  // refused. Nothing to say most of the time.
  notice: string | null;
};

// The one card in the centre column: paste text, see what it costs, press
// Narrate.
//
// The card decides for itself whether the Source is narratable, because the
// two reasons it is not — no text, or more text than one Narration takes —
// are both facts about the text in this textarea and both need saying in the
// same line the estimate uses.
export default function Composer({
  text,
  onTextChange,
  canNarrate,
  onNarrate,
  model,
  notice,
}: ComposerProps) {
  const estimate = estimateSource(text);
  const ready = canNarrate && estimate.narratable;

  return (
    <div className="composer">
      <label className="visually-hidden" htmlFor="source">
        Source
      </label>
      <textarea
        id="source"
        autoFocus
        aria-describedby="source-estimate"
        className="composer__source"
        value={text}
        placeholder="Paste or type anything…"
        onChange={(event) => onTextChange(event.target.value)}
      />

      <div className="composer__foot">
        <span
          id="source-estimate"
          className={`composer__estimate${estimate.overBy > 0 ? " composer__estimate--over" : ""}`}
        >
          {estimate.summary}
        </span>
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
        className={`composer__notice${notice ? "" : " composer__notice--quiet"}`}
        aria-live="polite"
      >
        {notice ?? ""}
      </p>
    </div>
  );
}
