import { useCallback, useEffect, useRef, useState } from "react";
import type { EngineClient, VoiceSelection } from "../engine/client";
import type { EngineBinding } from "../engine/useEngine";
import { rememberVoice } from "./lastVoice";

// The Voice the composer narrates with, and the one thing the pill can do
// about it.
export type VoiceBinding = {
  // The chosen Voice, or `null` before the Engine has said which it is. The
  // pill names no Voice until it knows one rather than guessing at the
  // Catalog's default, which is a guess that would be wrong for anyone who
  // has ever switched.
  selection: VoiceSelection | null;
  // Whether the Engine refused to say which Voice it holds. Not the same
  // `null` as a read still in flight: the composer narrates through the
  // first beat of a launch, but it will not promise a narration whose Voice
  // nobody can vouch for. Picking a Voice reads again, and so does the
  // Engine coming back from a restart; a stream that merely reconnects
  // does not.
  readFailed: boolean;
  // Why the last read or switch did not take. Cleared by the next one.
  notice: string | null;
  select: (chosen: VoiceSelection) => void;
};

const sentence = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

// What the shell knows of the Engine's stored choice.
type Known =
  | { state: "pending" }
  | { state: "read"; selection: VoiceSelection }
  | { state: "failed" };

// Binds the composer's Voice pill to the Engine's stored selection.
//
// The Engine is the one place the choice lives: it is read on connect and
// written the moment a reader switches, which is what makes a switch outlive
// the session it was made in. Nothing is kept here that the Engine does not
// already hold — a selection cached in the webview would be a second answer
// to "which Voice?", and the wrong one after a restart. What the webview
// does keep is the trail: which Voice each Voice Model was last left on,
// noted from the Engine's replies so it never disagrees with them.
//
// The state moves only on the Engine's reply, never optimistically. The
// write is a loopback request against a local database, and a pill that
// changed first would be showing a choice the Engine may yet refuse.
export const useVoice = (
  client: EngineClient,
  engine: EngineBinding,
): VoiceBinding => {
  const [known, setKnown] = useState<Known>({ state: "pending" });
  const [notice, setNotice] = useState<string | null>(null);

  const connected = engine.connection.state === "ready";

  // The read in flight, so a switch the Engine has stored since it was asked
  // can wave its answer off. Whatever that read says, the reader's pick is
  // newer, and its refusal was for a question nobody is still asking.
  const reading = useRef<{ superseded: boolean } | null>(null);

  useEffect(() => {
    if (!connected) return;
    const read = { superseded: false };
    reading.current = read;

    void client.voiceSelection().then(
      (chosen) => {
        if (read.superseded) return;
        rememberVoice(chosen);
        setNotice(null);
        setKnown({ state: "read", selection: chosen });
      },
      (error: unknown) => {
        if (read.superseded) return;
        setNotice(sentence(error, "The chosen Voice could not be read."));
        setKnown({ state: "failed" });
      },
    );

    return () => {
      read.superseded = true;
    };
  }, [client, connected]);

  const select = useCallback(
    (chosen: VoiceSelection) => {
      setNotice(null);
      // Only the read already in flight is older than this pick. One the
      // Engine starts after a restart is newer: whichever of the two answers
      // last is the Engine's latest word, and that is the one kept.
      const older = reading.current;
      // The reply, not the request: the Engine answers with the choice
      // resolved against the Catalog, and that is the pair every other
      // screen will see after a restart.
      void client.selectVoice(chosen).then(
        (stored) => {
          if (older) older.superseded = true;
          rememberVoice(stored);
          setNotice(null);
          setKnown({ state: "read", selection: stored });
        },
        (error: unknown) => {
          setNotice(sentence(error, "That Voice could not be chosen."));
        },
      );
    },
    [client],
  );

  return {
    selection: known.state === "read" ? known.selection : null,
    readFailed: known.state === "failed",
    notice,
    select,
  };
};
