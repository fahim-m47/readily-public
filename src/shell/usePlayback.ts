import { useCallback, useEffect, useRef, useState } from "react";
import type { EngineClient, HistoryNarration, HistoryNarrationAfter } from "../engine/client";
import type { EngineBinding } from "../engine/useEngine";
import { exportFileName } from "./readalong";

// The Narration the centre column is reading along with, and the two things
// the reader can do to the document itself rather than to the playback.
export type PlaybackBinding = {
  // The document on screen, or `null` for the composer. Either the
  // Narration the reader opened from History, or the one the Engine is
  // reading — a Narration started from the composer has no row to open.
  narration: HistoryNarration | null;
  // The document whose player is visible. Usually the document on screen;
  // while its live Narration is dismissed, the same player stays beside the
  // composer so playback controls do not disappear with the Read-along.
  playerNarration: HistoryNarration | null;
  // One sentence about the last thing the reader asked of the document, or
  // `null`. Never the Narration's own story: that is `describeNarration`'s,
  // and a notice that outranked it would hide a Narration failing.
  notice: string | null;
  // Put the composer back. Playback is untouched: leaving a Narration is not
  // stopping it, and the next Narration brings its own document.
  dismiss: () => void;
  // Undo a `dismiss` of one Narration, for a reader who asks for that
  // Narration again. Called on the way into `open`, because a row clicked is
  // a row wanted — without it a Narration closed once could never be
  // reopened. Named rather than wholesale: clearing every dismissal while
  // the open is still in flight would show the Narration the reader just
  // closed, which is the one the shell falls back to in the meantime.
  reveal: (narrationId: string) => void;
  // Read the open document's detail again, for a change the snapshot cannot
  // announce: a take swap replaces a Segment's offsets without moving `totalSec`.
  reread: () => void;
  exportAudio: () => void;
};

const sentence = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

// A sentence about one Narration, tagged so it cannot follow the reader to
// the next one.
type Tagged = { id: string; sentence: string };

// The detail in hand, tagged with the `reread` count it was read under so a
// later read can tell "one more Block assembled" from "read it all again".
type Held = { narration: HistoryNarration; asked: number };

// The cursor for reading only what has changed: the last ordinal of the
// contiguous measured prefix, or `null` when nothing is measured yet. A
// Segment with both `timelineStartSec` and `durationSec` is measured. A gap
// is not, so the cursor stops before it and a Resume that fills it is read
// along with every Block after it.
const settledCursor = ({ segments }: HistoryNarration) => {
  let cursor: number | null = null;
  for (const [index, segment] of segments.entries()) {
    const settled = segment.timelineStartSec !== null && segment.durationSec !== null;
    if (segment.ordinal !== index || !settled) break;
    cursor = segment.ordinal;
  }
  return cursor;
};

// The prefix is ours and the rest is the Engine's, unless the Engine sent
// the prefix back: it does when a Block in it lost its measurement under us
// (docs/wire.md, `afterOrdinal`), and an Engine that predates the cursor
// answers with every Block too. Either way its list is the whole truth.
const merged = (
  prior: HistoryNarration,
  cursor: number,
  fresh: HistoryNarrationAfter,
): HistoryNarration => ({
  ...fresh,
  source: prior.source,
  segments: fresh.segments.some((segment) => segment.ordinal <= cursor)
    ? fresh.segments
    : [...prior.segments.filter((segment) => segment.ordinal <= cursor), ...fresh.segments],
});

// Binds the read-along view to whichever Narration is in front of the reader.
//
// The Engine's `/v1/events` snapshot carries a playhead but not the text it
// is moving through, and the Block offsets that map one to the other live on
// the History detail. So this reads that detail — and rereads it as the
// Engine assembles, to learn offsets as cached lengths become available.
//
// The reread is paced by `totalSec`, not by `positionSec`. The playhead is
// patched at 20Hz and would mean a request every 50ms for a document that
// has not changed; `totalSec` moves once per Block, which is precisely when
// newly generated lengths become available. An unopened, unplaying Narration is not
// polled at all.
export const usePlayback = (
  client: EngineClient,
  engine: EngineBinding,
  opened: HistoryNarration | null,
): PlaybackBinding => {
  const [detail, setDetail] = useState<HistoryNarration | null>(null);
  // The same detail, reachable from the reading effect without being one of
  // its dependencies: a read that depended on its own result would loop.
  const inHand = useRef<Held | null>(null);
  // Tagged with its document for the same reason `readFailure` is: an
  // Export is asked of one Narration, and a sentence about it must not
  // travel to the next one — least of all to sit in front of that one's own
  // trouble.
  const [exportNotice, setExportNotice] = useState<Tagged | null>(null);
  // Tagged with the document it is about, the same way `detail` is. A read
  // that failed is a fact about one Narration, so it leaves with that
  // Narration instead of following the reader to the next one.
  const [readFailure, setReadFailure] = useState<Tagged | null>(null);
  // Every Narration the reader has closed and not asked for since. A set
  // rather than the last one closed: the Engine's Narration is the fallback
  // whenever no row is open, so forgetting it was closed when a second row
  // is closed would put it back on screen unasked.
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [held, setHeld] = useState<string | null>(null);

  const activeId = engine.narration?.narrationId ?? null;

  // Stopping is not leaving. The Engine reports `narrationId: null` the
  // instant a Narration stops, but its words are still on screen and the
  // reader is still looking at them — so the last Narration the Engine named
  // is held onto, and only the reader or the next Narration takes it away.
  //
  // Adjusted during render rather than in an effect: React's own pattern for
  // state derived from a changing input. An effect would render the document
  // gone for one frame and then bring it back, which is a flicker on every
  // Stop.
  if (activeId !== null && activeId !== held) setHeld(activeId);

  // A Narration the reader opened outranks the one playing: opening a
  // second row while the first still reads is a reader asking to look at
  // the second, and the player goes on driving whatever the Engine has.
  const wanted = opened?.id ?? activeId ?? held;
  const documentId = wanted !== null && dismissed.has(wanted) ? null : wanted;
  // The document whose player is visible: the one on screen, or the one the
  // Engine is reading while its own document is dismissed.
  const playerId = documentId ?? activeId;
  // Zero for a document that is not the one playing, so a stored Narration
  // is read once and then left alone.
  const assembledSec =
    playerId !== null && playerId === activeId
      ? (engine.narration?.totalSec ?? 0)
      : 0;

  const [asked, setAsked] = useState(0);
  const reread = useCallback(() => setAsked((count) => count + 1), []);

  useEffect(() => {
    if (playerId === null) return;
    let live = true;

    const prior = inHand.current;
    const cursor =
      prior !== null && prior.narration.id === playerId && prior.asked === asked
        ? settledCursor(prior.narration)
        : null;
    const read =
      prior !== null && cursor !== null
        ? client
            .openNarration(playerId, { afterOrdinal: cursor })
            .then((fresh) => merged(prior.narration, cursor, fresh))
        : client.openNarration(playerId);

    void read.then(
      (narration) => {
        if (!live) return;
        // A reread that lands clears the last one that did not. The Engine
        // assembles for as long as a Narration runs, so one dropped request
        // must not leave a sentence under a player that is visibly reading.
        setReadFailure(null);
        inHand.current = { narration, asked };
        setDetail(narration);
      },
      (error: unknown) => {
        if (!live) return;
        setReadFailure({
          id: playerId,
          sentence: sentence(error, "That Narration could not be read."),
        });
      },
    );

    return () => {
      live = false;
    };
  }, [client, playerId, assembledSec, asked]);

  // Between a document changing and its detail arriving, the last one is
  // still in state. Rendering it would put the previous Narration's text
  // under the new one's playhead, so it is filtered by id rather than
  // cleared — `opened` already holds the same detail, and using it means
  // reopening a row shows its words in the same frame as the click.
  const playerNarration =
    (detail?.id === playerId ? detail : null) ??
    (opened?.id === playerId ? opened : null);
  const narration = documentId === null ? null : playerNarration;

  const dismiss = useCallback(() => {
    setExportNotice(null);
    if (wanted === null) return;
    setDismissed((closed) =>
      closed.has(wanted) ? closed : new Set(closed).add(wanted),
    );
  }, [wanted]);

  const reveal = useCallback((narrationId: string) => {
    setDismissed((closed) => {
      if (!closed.has(narrationId)) return closed;
      const rest = new Set(closed);
      rest.delete(narrationId);
      return rest;
    });
  }, []);

  const exportAudio = useCallback(() => {
    if (!playerNarration) return;
    setExportNotice(null);
    void client
      .exportNarration(playerNarration.id, {
        defaultName: exportFileName(playerNarration.sourcePreview),
      })
      .then(
        (started) => {
          // `false` is the reader dismissing the save panel, which is not
          // an outcome worth a sentence. `true` is the Engine accepting the
          // Export, not the file existing — writing it can mean re-making
          // audio retention swept away, which queues behind what is playing.
          if (started) {
            setExportNotice({
              id: playerNarration.id,
              sentence: "Saving the audio…",
            });
          }
        },
        (error: unknown) => {
          setExportNotice({
            id: playerNarration.id,
            sentence: sentence(error, "That Narration could not be exported."),
          });
        },
      );
  }, [client, playerNarration]);

  // The reader's own last request outranks the document's own trouble: an
  // Export they asked for and did not get is the more urgent of the two.
  const about = (tagged: Tagged | null) =>
    tagged !== null && tagged.id === playerNarration?.id
      ? tagged.sentence
      : null;
  const notice = about(exportNotice) ?? about(readFailure);

  return { narration, playerNarration, notice, dismiss, reveal, reread, exportAudio };
};
