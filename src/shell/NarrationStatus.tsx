import { LoaderCircle } from "lucide-react";
import type { ShellStatus } from "./lifecycle";

export type NarrationStatusProps = {
  status: ShellStatus | null;
  // A player is on screen and tells the story itself — Pause for reading,
  // Play for paused or finished, its own line for a problem — so the line
  // here is for screen readers only. It stays mounted and live either way,
  // because a region that arrives already holding its first message is
  // announced unreliably.
  quiet?: boolean;
};

// The prepare line: the Engine's lifecycle — the wait for a Voice Model, then
// the wait for the first words — reaching a reader as a sentence, never as a
// meter, a Block table, or a percentage nobody can act on. Once the player's
// transport can tell the story, this row goes on saying it to the software
// that reads the screen aloud, and the player shows a problem itself.
export default function NarrationStatus({
  status,
  quiet = false,
}: NarrationStatusProps) {
  // The live region stays mounted even with nothing to say. A region that
  // arrives already holding its first message is announced unreliably — the
  // text has to change *inside* a region the reader's software is already
  // watching.
  return (
    <div
      className={`narration narration--${status?.tone ?? "idle"}${quiet ? " narration--quiet" : ""}`}
    >
      <p
        className={`narration__line${quiet ? " visually-hidden" : ""}`}
        aria-live="polite"
      >
        {status?.tone === "working" && (
          <LoaderCircle aria-hidden="true" className="narration__spinner" />
        )}
        {status?.message ?? ""}
      </p>
    </div>
  );
}
