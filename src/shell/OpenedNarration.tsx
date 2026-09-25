import { Fragment, memo, useEffect, useRef, type RefObject } from "react";
import { activeBlockIndex, type ReadAlongBlock, type ReadPiece } from "./readalong";
import type { SourceTiming } from "../engine/client";

export type OpenedNarrationProps = {
  // The Source cut into Blocks. Passed in rather than cut here, so the
  // scrubber and the highlight are working from one list and cannot come to
  // different conclusions about what a Block is.
  blocks: readonly ReadAlongBlock[];
  // The Engine's playhead, in seconds, or `null` when nothing is reading
  // this Narration. Not a clock this component runs — it is a snapshot, and
  // the highlight moves only when a new one arrives.
  positionSec: number | null;
  // Ask the Engine to move its playhead to this Block's start, or `null`
  // when this document is not the one the Engine is reading. The Engine
  // seeks whatever Narration is active, so a Block click here while another
  // Narration plays would jump *that* audio to an offset measured on this
  // document's clock. Without a seek to call, the Blocks render as words
  // rather than as controls — which is also the honest thing to show.
  onSeek: ((sourceOffset: number) => void) | null;
};

// The read-along view highlights the current Block and makes Blocks with
// known prefixes clickable, including targets that still need synthesis.
// Clicks send Source coordinates so the Engine owns the seek calculation.
//
// The Blocks are laid out inline, not as boxes. The Source's own line breaks
// are its only structure and the container preserves them, so a Block that
// claimed a line of its own would insert paragraph breaks the reader never
// typed — a Block is a unit of synthesis, not a unit of layout.
//
// It also stays where gaps become the words they cost, which is the only
// place a reader can judge what they missed (ADR 0002 §8).
//
// Threat model B4: the Source is data the reader pasted from somewhere they
// do not control. It is rendered as text nodes, through React's own
// escaping. Nothing here may hand the Source to React's raw-HTML escape
// hatch — the `.semgrep` frontend rule blocks it.
// A trackpad click can drag a single character, so one held character
// still counts as a click rather than a selection.
const holdsSelection = () =>
  (window.getSelection?.()?.toString().length ?? 0) > 1;

export default function OpenedNarration({
  blocks,
  positionSec,
  onSeek,
}: OpenedNarrationProps) {
  // `null` is not zero. A document nobody is reading has no playhead at all,
  // and treating it as a playhead at the start would highlight the first
  // Block as being read when no audio is running.
  const active = positionSec === null ? -1 : activeBlockIndex(blocks, positionSec);
  // Counted from what is actually about to be marked rather than from the
  // gap records: `markGaps` drops ranges that fall outside the Source and
  // merges ones that overlap, and a banner promising marks the text does not
  // carry is the inverse of what ADR 0002 §8 asks of us.
  const marked = blocks.reduce(
    (total, block) => total + block.pieces.filter((piece) => piece.skipped).length,
    0,
  );

  const reading = useRef<HTMLElement>(null);
  useEffect(() => {
    // Following the audio is the whole point of a read-along view: a
    // highlight that walks off the bottom of a long Source leaves the reader
    // chasing it. `scrollIntoView` is absent in jsdom, hence the optional
    // call — the tests assert the highlight, not the scrolling.
    reading.current?.scrollIntoView?.({
      behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
      block: "center",
    });
  }, [active]);

  return (
    <article className="opened" aria-label="Opened Narration">
      {marked > 0 && (
        <p className="opened__gaps">
          {marked === 1
            ? "One passage could not be read and was skipped."
            : `${marked} passages could not be read and were skipped.`}{" "}
          They are marked in the text.
        </p>
      )}

      <p className="opened__source">
        {blocks.map((block, index) => {
          const current = index === active;
          return (
            <Block
              key={block.start}
              pieces={block.readPieces}
              seek={block.seekSourceOffset === null ? null : onSeek}
              current={current}
              played={active >= 0 && index < active}
              currentWord={
                current && positionSec !== null
                  ? block.timings.find(
                      (word) => word.startSec <= positionSec && positionSec < word.endSec,
                    )
                  : undefined
              }
              reading={reading}
            />
          );
        })}
      </p>
    </article>
  );
}

type BlockProps = {
  pieces: ReadPiece[];
  seek: ((sourceOffset: number) => void) | null;
  current: boolean;
  played: boolean;
  currentWord: SourceTiming | undefined;
  reading: RefObject<HTMLElement | null>;
};

// Memoized so a playhead snapshot re-renders only the Block it left and the
// Block it entered.
const Block = memo(function Block({ pieces, seek, current, played, currentWord, reading }: BlockProps) {
  return (
    <span
      className={`opened__block${played ? " opened__block--played" : ""}`}
      // `aria-current` rather than `aria-selected`: this is the one
      // Block out of many that the reader is at, which is what
      // `current` means. Absent, not `false`, on the others —
      // `aria-current="false"` is announced by some software. Once a
      // word inside it is current, the word carries the mark instead.
      aria-current={current && !currentWord ? "true" : undefined}
      ref={current ? reading : undefined}
    >
      {pieces.map((piece) =>
        piece.skipped ? (
          <mark
            className="opened__skipped"
            key={piece.sourceOffset}
            title="Skipped — this could not be read"
          >
            <span className="visually-hidden">Start of skipped text: </span>
            {piece.text}
            <span className="visually-hidden"> End of skipped text.</span>
          </mark>
        ) : (
          <span key={piece.sourceOffset}>
            {piece.runs.map((run) =>
              !run.word || seek === null ? (
                <Fragment key={run.sourceOffset}>{run.text}</Fragment>
              ) : (
                <button
                  className="opened__block opened__block--seekable"
                  aria-current={run.timing === currentWord ? "true" : undefined}
                  key={run.sourceOffset}
                  title={
                    run.timing === null
                      ? "Generate and play from this word"
                      : run.timing.provenance === "estimated"
                        ? "Play from near this word"
                        : "Play from this word"
                  }
                  onClick={() => {
                    if (!holdsSelection()) seek(run.sourceOffset);
                  }}
                  type="button"
                >
                  <span className="opened__words">{run.text}</span>
                </button>
              ),
            )}
          </span>
        ),
      )}
    </span>
  );
});
