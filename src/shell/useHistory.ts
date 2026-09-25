import { useCallback, useEffect, useState } from "react";
import type {
  EngineClient,
  Mode,
  HistoryEntry,
  HistoryNarration,
} from "../engine/client";
import type { EngineBinding } from "../engine/useEngine";
import { isReplayable } from "./history";
import { exportFileName } from "./readalong";

// The shell's live view of History and its actions.
export type HistoryBinding = {
  // Newest first, as the Engine ordered them.
  entries: HistoryEntry[];
  // The Narration reopened in the centre column, or `null` for the composer.
  opened: HistoryNarration | null;
  // One sentence about the last thing History did — a request the Engine
  // refused. A delete that worked says nothing.
  notice: string | null;
  open: (narrationId: string, mode: Mode) => void;
  remove: (narrationId: string) => void;
  exportAudio: (entry: HistoryEntry) => void;
  close: () => void;
};

const sentence = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

// Binds History to the one EngineClient the shell already holds.
//
// History has no stream of its own, so every reason it might have changed is
// a dependency of the one effect that reads it: the Engine came up, the
// active Narration moved between phases, or the reader deleted something.
// That is how an evicted row's marker clears without the shell ever editing
// a row — the replay finishes, the phase changes, History is read again.
//
// Only for a Narration that replays whole, though. A `finished` one does; an
// `interrupted` or `stopped` one resumes at its stored playhead, and the
// Engine skips the Blocks ending before it (`docs/wire.md`), so the Segments
// retention swept out of the part already heard stay missing and the row
// goes on saying so. That row is telling the truth: re-synthesizing audio
// nobody asked to hear again is not the shell's call to make.
export const useHistory = (
  client: EngineClient,
  engine: EngineBinding,
): HistoryBinding => {
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [opened, setOpened] = useState<HistoryNarration | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  const connected = engine.connection.state === "ready";
  const phase = engine.narration?.phase ?? null;
  const activeId = engine.narration?.narrationId ?? null;

  useEffect(() => {
    if (!connected) return;
    // Phase changes can land faster than a listing comes back, so a reply
    // whose read has been superseded publishes nothing: it would put a stale
    // History — with the marker the reader just cleared — back on screen.
    let live = true;

    void client.listHistory().then(
      (listing) => {
        if (live) setEntries(listing);
      },
      (error: unknown) => {
        if (live) setNotice(sentence(error, "History could not be read."));
      },
    );

    return () => {
      live = false;
    };
  }, [client, connected, phase, activeId, revision]);

  const open = useCallback(
    (narrationId: string, mode: Mode) => {
      setNotice(null);
      void (async () => {
        try {
          const narration = await client.openNarration(narrationId);
          setOpened(narration);
          // Opened paused: a row clicked is a document to look at, and the
          // player's play button is where sound starts. A Narration the
          // Engine would refuse — one already active, or a failed one — is
          // reopened without becoming active at all. Its Source and its
          // gaps are the point of opening it either way.
          if (isReplayable(narration.status)) {
            await client.resumeNarration(narrationId, mode, { paused: true });
          }
        } catch (error) {
          setNotice(sentence(error, "That Narration could not be opened."));
        }
      })();
    },
    [client],
  );

  const remove = useCallback(
    (narrationId: string) => {
      void (async () => {
        try {
          await client.deleteNarration(narrationId);
          setOpened((current) => (current?.id === narrationId ? null : current));
          // A row that has gone is the whole answer; the freed disk is in
          // Settings for a reader who wants the number.
          setNotice(null);
          setRevision((previous) => previous + 1);
        } catch (error) {
          setNotice(sentence(error, "That Narration could not be deleted."));
        }
      })();
    },
    [client],
  );

  const close = useCallback(() => setOpened(null), []);

  const exportAudio = useCallback(
    (entry: HistoryEntry) => {
      setNotice(null);
      void client.exportNarration(entry.id, {
        defaultName: exportFileName(entry.sourcePreview),
      }).then(
        (started) => {
          if (started) setNotice("Saving the audio…");
        },
        (error: unknown) => {
          setNotice(sentence(error, "That Narration could not be exported."));
        },
      );
    },
    [client],
  );

  return { entries, opened, notice, open, remove, exportAudio, close };
};
