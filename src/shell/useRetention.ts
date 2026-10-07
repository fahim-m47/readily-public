import { useCallback, useEffect, useRef, useState } from "react";
import type { EngineClient, RetentionPolicy, RetentionSettings } from "../engine/client";
import { describeEviction } from "./retention";

// One knob's new value, which is all any control here can honestly report.
// A change is never a pair: composing a pair out of what the sheet last
// rendered lets a budget the reader is in the middle of raising ride along
// with a keep-audio window and quietly put the old one back — and a budget
// the Engine applies deletes audio, so putting the old one back is not a
// display bug the next render corrects.
//
// The `never` arms are what make that structural. Excess property checking
// against a bare union only rejects a key no member has, so without them
// `{ segmentBudgetBytes, keepAudioDays }` type-checks and the defect above
// is one careless edit away from returning.
export type RetentionChange =
  | { segmentBudgetBytes: number; keepAudioDays?: never }
  | { keepAudioDays: number | null; segmentBudgetBytes?: never };

// The two retention knobs, the disk they govern, and the two things the
// Settings sheet can do about them.
export type RetentionBinding = {
  // What the Engine has stored, or `null` before it has said. `null` is not
  // "the defaults": showing 5 GB before the Engine has been asked would put
  // a number on screen that a reader who lowered it last week would rightly
  // read as their setting having been thrown away.
  settings: RetentionSettings | null;
  // Why there is nothing to show. Kept apart from `notice` so the sheet
  // never says both that it is still reading and that the read failed.
  failure: string | null;
  // The newest thing that happened to the reader: the audio a lowered
  // budget removed, or the reason a change did not take.
  notice: string | null;
  // Write the one knob that moved. The reply is the truth — including the
  // bytes the Engine's sweep removed, which is what makes a budget drop
  // something the reader can watch rather than something they are promised.
  save: (change: RetentionChange) => void;
  // Show the folder every Export lands in, Documents/Readily, in the Finder.
  openAudioFolder: () => void;
  // Show the data folder in the Finder.
  openFolder: () => void;
  // Show the log file in the Finder, selected, so what gets dragged into a
  // bug report is the file and not the folder that holds the Sources.
  revealLogs: () => void;
};

// The Engine's errors arrive as `Error`s; the shell's Finder commands
// reject with the Rust `Err` as Tauri serialised it, a plain string.
const sentence = (error: unknown, fallback: string) => {
  if (typeof error === "string") return error;
  return error instanceof Error ? error.message : fallback;
};

// The two knobs alone, out of a reply that also carries the disk and what
// the sweep just removed. `confirmed` below is a merge base rather than
// something to render, and a disk figure kept on it would be a number that
// moved the moment the next write answered.
const policyOf = ({
  segmentBudgetBytes,
  keepAudioDays,
}: RetentionPolicy): RetentionPolicy => ({ segmentBudgetBytes, keepAudioDays });

// The Engine's retention policy as the shell tracks it, and the single file
// of work that advances it.
//
// Keyed to the client rather than held in the hook, because the Settings
// sheet is not the unit this belongs to. A reader can lower the budget,
// press Done while the write is still out, reopen Settings and turn the
// other knob — and a per-sheet queue would let that second sheet, which is
// rendering the policy from before the first write, race the write it knows
// nothing about and resend the budget the reader replaced.
type Tracked = {
  // The newest policy the Engine has confirmed, or `null` when nothing has
  // been confirmed yet or a failure made the last answer untrustworthy.
  confirmed: RetentionPolicy | null;
  // How many writes have been asked for and not yet answered. A read is
  // the freshest truth only while that is zero; a write in flight will
  // answer with something newer than a question asked before it.
  outstanding: number;
  // Every write this client has been asked for, in order, so each merges
  // against the reply to the one before it.
  //
  // Writes only. A read on this queue would make one call the Engine
  // accepts and never answers — `client` sets no request timeout — brick
  // Settings for the life of the process: every later sheet would sit on
  // "Reading your settings…" behind a slot that never completes. A read
  // ahead of a pending write shows a value that write is about to change,
  // which is the same momentary staleness this sheet already accepts by
  // refusing to move anything optimistically.
  queue: Promise<void>;
};

const tracking = new WeakMap<EngineClient, Tracked>();

const trackedFor = (client: EngineClient): Tracked => {
  const known = tracking.get(client);
  if (known) return known;
  const fresh: Tracked = { confirmed: null, outstanding: 0, queue: Promise.resolve() };
  tracking.set(client, fresh);
  return fresh;
};

// Binds the Settings sheet to the Engine's stored retention policy.
//
// Read on mount rather than on connect, because the sheet is mounted only
// while it is open: opening Settings is the moment the numbers matter, and
// re-reading then is what keeps the disk usage current after a download or
// a delete moved it.
//
// Nothing moves optimistically. A budget the shell showed before the Engine
// applied it would be a number the reader is invited to check — against a
// disk usage that had not moved yet.
//
// Which is exactly why a write cannot be composed from what is on screen.
// While a write is in flight the sheet is still rendering the previous
// policy, so a second knob turned in that window would carry the first
// one's old value back to the Engine. Each control therefore reports only
// the knob it moved, and [`Tracked`] merges it against the reply to the
// write before it.
//
// `client` has to be the same object across renders for that to mean
// anything — [`Tracked`] is keyed on its identity, and a client rebuilt
// each render would get a fresh one and lose the merge without failing.
// `App` passes the module-level client `main.tsx` builds once, which is
// what makes this hold.
export const useRetention = (client: EngineClient): RetentionBinding => {
  const [settings, setSettings] = useState<RetentionSettings | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Whether this sheet is still open. A write outlives the Done button, and
  // its reply has nowhere to be reported once the sheet is gone — but it
  // still has to reach [`Tracked`], which is why this gates the `set…`
  // calls and nothing else.
  const open = useRef(true);
  useEffect(() => {
    open.current = true;
    return () => {
      open.current = false;
    };
  }, []);

  // What the Engine is enforcing, after a write whose outcome the shell
  // cannot tell. A refusal says nothing about whether the sweep ran before
  // it: a rejected body never reached the janitor, and a dropped connection
  // may have.
  const reconcile = useCallback(
    async (tracked: Tracked) => {
      try {
        const read = await client.retention();
        tracked.confirmed = policyOf(read);
        if (open.current) setSettings(read);
      } catch (error: unknown) {
        // Nothing here is known to be stored any more, and the reader
        // checks these numbers against their own disk. Better an empty
        // sheet than two figures the Engine may have moved past.
        tracked.confirmed = null;
        if (!open.current) return;
        setSettings(null);
        // This supersedes the write's own notice rather than joining it:
        // "that did not save" is a detail of a sheet that can no longer
        // say what did.
        setNotice(null);
        setFailure(sentence(error, "The retention settings could not be read."));
      }
    },
    [client],
  );

  useEffect(() => {
    const tracked = trackedFor(client);
    // Local to this effect run rather than a ref, so a read left over from
    // an abandoned run cannot answer for the run that replaced it.
    let live = true;

    void client.retention().then(
      (read) => {
        // Only while nothing newer is on its way. A write in flight will
        // answer with a policy this question was asked before.
        if (tracked.outstanding === 0) tracked.confirmed = policyOf(read);
        if (!live) return;
        setFailure(null);
        setSettings(read);
      },
      (error: unknown) => {
        if (live) {
          setFailure(sentence(error, "The retention settings could not be read."));
        }
      },
    );

    return () => {
      live = false;
    };
  }, [client]);

  const save = useCallback<RetentionBinding["save"]>(
    (change) => {
      setNotice(null);
      const tracked = trackedFor(client);
      tracked.outstanding += 1;
      tracked.queue = tracked.queue
        .then(async () => {
          try {
            // Read rather than give up when nothing is confirmed: a failed
            // reconcile leaves `confirmed` empty, and a save that quietly
            // did nothing would be the worst of the three answers.
            const base = tracked.confirmed ?? policyOf(await client.retention());
            // Merged field by field rather than spread. `never` keeps a
            // *value* out of the arm it does not belong to, but with
            // `exactOptionalPropertyTypes` off it still admits an explicit
            // `undefined` — and a spread would let that beat the confirmed
            // value and send the Engine a body missing a field it requires.
            const applied = await client.setRetention({
              segmentBudgetBytes: change.segmentBudgetBytes ?? base.segmentBudgetBytes,
              keepAudioDays:
                change.keepAudioDays === undefined
                  ? base.keepAudioDays
                  : change.keepAudioDays,
            });
            tracked.confirmed = policyOf(applied);
            if (!open.current) return;
            setSettings(applied);
            setNotice(describeEviction(applied.evictedBytes));
          } catch (error: unknown) {
            if (open.current) {
              setNotice(sentence(error, "That setting could not be saved."));
            }
            await reconcile(tracked);
          } finally {
            tracked.outstanding -= 1;
          }
        })
        // A rejection here would leave `queue` permanently rejected, and
        // every later change would be dropped without being attempted.
        .catch(() => {});
    },
    [client, reconcile],
  );

  const openAudioFolder = useCallback(() => {
    setNotice(null);
    void client.openAudioFolder().catch((error: unknown) => {
      setNotice(sentence(error, "The audio folder could not be opened."));
    });
  }, [client]);

  const openFolder = useCallback(() => {
    setNotice(null);
    void client.openDataFolder().catch((error: unknown) => {
      setNotice(sentence(error, "The data folder could not be opened."));
    });
  }, [client]);

  const revealLogs = useCallback(() => {
    setNotice(null);
    void client.revealLogs().catch((error: unknown) => {
      setNotice(sentence(error, "The log file could not be shown."));
    });
  }, [client]);

  return { settings, failure, notice, save, openAudioFolder, openFolder, revealLogs };
};
