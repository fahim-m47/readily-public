// The Engine's lifecycle, in plain language.
//
// Wording here is deliberately plain rather than glossary-exact: CONTEXT.md's
// vocabulary (Block, Segment, Voice Model) is how this codebase talks to
// itself, and these lines talk to a listener instead.

import type { Connection, NarrationState } from "../engine/client";

// `tone` is what the shell colours the line by, not a second message.
export type ShellStatus = {
  tone: "working" | "ready" | "failed";
  message: string;
};

// The supervisor's boot beats, named.
//
// `provisioning` is the multi-minute first-run environment build (ADR 0001
// §6) and `restarting` is a crash the supervisor is recovering from, so
// collapsing them into one "starting" would hide the two waits a reader most
// needs explained.
//
// `ready` means `/health` answered, which is the moment Uvicorn is up. Boot
// prewarm is not behind that: the Engine queues it on its generation worker
// and starts serving in the same breath (`serve.py`, `narration/worker.py`),
// so the default Voice Model is still loading for a while after this line
// clears. `describeNarration` is where that wait is reported.
export const describeConnection = (connection: Connection): ShellStatus => {
  if (connection.state === "failed") {
    return { tone: "failed", message: connection.message };
  }
  if (connection.state === "ready") {
    return { tone: "ready", message: "Engine ready" };
  }
  switch (connection.detail) {
    case "provisioning":
      return { tone: "working", message: "Setting up the Engine…" };
    case "restarting":
      return { tone: "working", message: "Restarting the Engine…" };
    default:
      return { tone: "working", message: "Starting the Engine…" };
  }
};

// The active Narration's line, or `null` when there is no Narration to
// narrate about.
//
// `preparing` gets one sentence because the Engine reports one phase that
// covers two waits — a Voice Model still loading, and the generation of the
// first Block — and the wire does not say which is happening. Splitting it
// needs a phase the Engine reports.
export const describeNarration = (
  narration: NarrationState | null,
): ShellStatus | null => {
  if (narration?.error?.code === "generation_gap") {
    return { tone: "failed", message: narration.error.message };
  }
  switch (narration?.phase) {
    case "preparing":
      return { tone: "working", message: "Getting the words ready…" };
    case "playing":
      return { tone: "working", message: "Reading aloud…" };
    case "paused":
      return { tone: "ready", message: "Paused." };
    case "finished":
      return { tone: "ready", message: "Finished reading." };
    case "failed":
      return {
        tone: "failed",
        message: narration.error?.message ?? "The Narration failed.",
      };
    default:
      return null;
  }
};

// Whether there is something for Stop to stop. The Engine takes a stop in
// all three of these phases and answers `stopped: false` anywhere else.
export const isStoppable = (narration: NarrationState | null) =>
  narration?.phase === "preparing" ||
  narration?.phase === "playing" ||
  narration?.phase === "paused";

// Whether the Engine is, or is about to be, making sound: what opening
// another row would cut off, and what holds the Mode still. A paused
// Narration is stoppable but not reading — a row opens paused, so a reader
// browsing History is never asked about a Narration they have not heard.
export const isReading = (narration: NarrationState | null) =>
  narration?.phase === "preparing" || narration?.phase === "playing";

// Whether moving on would cut generation short: what the reader is asked
// about before another row, a New Narration, or narrating something else
// cancels it. Playing counts only while the Engine still has Blocks to
// make, so playback of audio it already finished is never a question. A
// paused Narration is left out as in `isReading`: a row opens paused, and
// a streaming Narration paused mid-generation only parks at its lookahead.
export const isGenerating = (narration: NarrationState | null) =>
  narration?.phase === "preparing" ||
  (narration?.phase === "playing" && !narration.diagnostics.generationComplete);
