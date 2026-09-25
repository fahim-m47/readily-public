// The escape hatch out of a wait: the fastest installed Voice Model, offered
// while an expressive Narration is still being synthesized.

import type {
  CatalogEntry,
  HistoryNarration,
  ModelStatus,
  NarrationState,
  VoiceSelection,
} from "../engine/client";
import { sourceText } from "./source";
import { voiceToOffer } from "./lastVoice";
import { installedEntries } from "./voice";

// Tier is editorial and "deliberately not a scale" (CONTEXT.md), and this is
// the one place in the shell that reads it as one. A Tier this list has not
// heard of gets no rank at all, so it is neither offered nor overtaken —
// guessing could point the offer at a *slower* Voice Model.
const TIERS_FASTEST_FIRST: readonly string[] = ["instant", "expressive"];

// Where a Tier sits in that order, lowest first, or `null` for one this
// shell has never heard of.
const tierRank = (tier: string) => {
  const rank = TIERS_FASTEST_FIRST.indexOf(tier);
  return rank === -1 ? null : rank;
};

// How much assembled-but-unheard audio counts as "comfortably ahead", past
// which nobody is waiting on synthesis. Grounded in the measured expressive
// TTFA of 4–7s warm, with headroom so a
// buffer that dips for one snapshot does not flash the offer back on screen.
const COMFORTABLE_LEAD_SEC = 15;

// Whether the Engine still has Source left to turn into audio.
//
// The playhead alone cannot answer this: `totalSec` stops at the Narration's
// full duration and `positionSec` climbs to meet it (docs/wire.md), so a thin
// lead is also what a fully assembled Narration looks like.
//
// Assembly runs in Block order and stamps a Block's timeline offset as it
// feeds it to playback, so the furthest stamped Block is the furthest point
// of the Source assembled. A skipped Block is never stamped, so a gap counts
// as reached.
const assembling = ({ source, segments, gaps }: HistoryNarration) => {
  const reached = segments.filter(
    (segment) =>
      segment.timelineStartSec !== null ||
      gaps.some((gap) => gap.ordinal === segment.ordinal),
  );
  const assembled = reached.reduce(
    (end, segment) => Math.max(end, segment.sourceEnd),
    0,
  );
  return assembled < sourceText(source).length;
};

// Whether the reader is currently waiting on synthesis.
//
// `preparing` counts only while there is something left to synthesize: a
// seek re-enters `preparing` to replay cached Segments (docs/wire.md).
// `playing` counts only while the playhead is close behind what has been
// assembled.
const synthesisBound = (
  narration: NarrationState,
  document: HistoryNarration,
) => {
  if (!assembling(document)) return false;
  if (narration.phase === "preparing") return true;
  if (narration.phase !== "playing") return false;
  return narration.totalSec - narration.positionSec < COMFORTABLE_LEAD_SEC;
};

// A Voice Model to escape to, named so the offer can say it out loud, and
// the words taking it would reread.
export type FasterVoice = {
  name: string;
  selection: VoiceSelection;
  // The Engine's own Source for the Narration being waited on — never the
  // composer's, which the reader may have edited since pressing Narrate.
  source: string;
};

// The offer to make while a Narration is synthesis-bound, or `null` when
// there is no honest one.
//
// `document` must be the shell's copy of the Narration the Engine is reading:
// it carries both the words a restart would reread and the evidence of how
// far synthesis has got. The Voice Model offered is always an installed one —
// a Catalog entry the disk does not have answers `409 model_not_installed`.
export const fasterVoice = (
  narration: NarrationState | null,
  document: HistoryNarration | null,
  entries: readonly CatalogEntry[] | null,
  statusOf: (entryId: string) => ModelStatus | undefined,
): FasterVoice | null => {
  if (narration === null || document === null) return null;
  if (document.id !== narration.narrationId) return null;
  if (!synthesisBound(narration, document)) return null;

  const reading = entries?.find((entry) => entry.id === narration.modelId);
  const current = reading ? tierRank(reading.tier) : null;
  if (current === null) return null;

  const faster = installedEntries(entries, statusOf)
    .flatMap(({ entry }) => {
      const rank = tierRank(entry.tier);
      return rank !== null && rank < current ? [{ entry, rank }] : [];
    })
    .sort((left, right) => left.rank - right.rank)[0];
  if (faster === undefined) return null;

  return {
    name: faster.entry.name,
    selection: {
      modelId: faster.entry.id,
      voiceId: voiceToOffer(faster.entry)?.id ?? faster.entry.defaultVoiceId,
    },
    source: document.source,
  };
};
