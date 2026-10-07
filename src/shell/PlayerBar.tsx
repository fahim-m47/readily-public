import { useState } from "react";
import type { NarrationState } from "../engine/client";
import { Pause, Play, RotateCcw, RotateCw } from "lucide-react";
import { VoiceOrb } from "./orb/VoiceOrb";
import type { OrbIdentity } from "./orb/identity";
import { formatPlayhead } from "./readalong";

const PLAYBACK_SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 3.5, 4] as const;

export type PlayerBarProps = {
  // The Engine's snapshot, but only when it is *this* document the Engine is
  // reading. `null` for a stored Narration nobody is playing: its transport
  // has nothing to drive, and borrowing another Narration's playhead would
  // put a clock under these words that is measuring different audio.
  narration: NarrationState | null;
  // The Narration's length. Its measured one once the Engine has finished
  // it; what has been assembled so far while it is still being read.
  totalSec: number;
  // One sentence about the last thing the reader asked of the document — an
  // Export that failed, say. Not the Narration's own story, which
  // `NarrationStatus` tells from the row under the player; this one is
  // announced, because it answers something the reader asked for and did not
  // get.
  notice: string | null;
  // What is wrong, if anything: the Narration's failure, or the Engine's
  // refusal of the last thing the reader asked. Shown under the transport
  // in a line that is `aria-hidden`, because `NarrationStatus` already
  // announces the same sentence from a region that is never remounted:
  // shown once, told once. Never the Narration's ordinary story — reading,
  // paused, finished — which the Pause and Play buttons tell.
  problem: string | null;
  // The Voice, named — "Kokoro · Heart" — or `null` before the Catalog has
  // been read. The orb is the Voice's own, derived from its ids until the
  // Catalog says where the Voice sits, so it is there even before the name.
  voice: string | null;
  orb: OrbIdentity;
  // The way out of a wait: a faster Voice Model already on disk, and what
  // taking it does. `null` — the posture the seek takes too — whenever there
  // is no honest offer, which is most of the time. Its own words say there is
  // a wait, so nothing else beside the Voice needs to.
  faster: { name: string; onTake: () => void } | null;
  onPause: () => void;
  // Start sound: release a pause, or read a finished Narration again from
  // the top. Which one is the Engine's phase to say, not this component's.
  onPlay: () => void;
  // Move the playhead to a second, or `null` when this document is not the
  // one the Engine is reading. `null` rather than a boolean beside the
  // callback, because the Engine seeks *the active Narration* and has no
  // idea which document a reader is looking at: handing this component a
  // seek it must remember not to call is how a scrubber ends up moving
  // another Narration's audio. With no seek to call there is nothing to
  // forget.
  onSeekTime: ((positionSec: number) => void) | null;
  // The Engine's persisted playback speed. One setting for every Narration,
  // so it is shown and changeable under a stored document too, unlike the
  // seek, which only ever moves the active Narration.
  speed: number;
  onSpeed: (speed: number) => void;
  // Stop the Engine's work on this Narration, resolving `true` once the
  // Engine has taken the stop. `null` while there is nothing in flight to
  // cancel. The button says "Cancelling…" and takes no second press until
  // the stop is refused or this goes back to `null`.
  onCancel: (() => Promise<boolean>) | null;
};

// The docked player: everything a reader does to a Narration that is not
// clicking into its text.
//
// Every control here reflects the Engine's phase rather than a state of its
// own. There is no optimistic pause, no local clock, and no disabled-state
// guessing: the button says Pause because the Engine last said `playing`,
// and it says Play the moment the Engine says `paused`. That is what makes
// inspecting the Engine mid-pause show one playhead and not two.
//
// The one exception is the scrubber mid-drag, and it is not a second
// playhead: it is where the reader's thumb is, which the Engine cannot know
// until they let go.
//
// A finished Narration is not a dead one. Its transport stays live: Play
// reads it again from the top, and moving the playhead — the scrubber, the
// skip back — reads it again from there. What those do is the caller's
// (`App`) to arrange with the Engine; here they are the same buttons.
export default function PlayerBar({
  narration,
  totalSec,
  notice,
  problem,
  voice,
  orb,
  faster,
  onPause,
  onPlay,
  onSeekTime,
  speed,
  onSpeed,
  onCancel,
}: PlayerBarProps) {
  // Where the thumb is while it is being dragged, and `null` whenever it is
  // not. A range input fires `change` on every intermediate value, so
  // committing there would send one seek per pixel — and each seek makes the
  // Engine discard its queued audio and re-prepare, which is both wasted
  // work and a playhead that never settles. The reader means the value they
  // let go of, so that is the one that travels.
  const [dragging, setDragging] = useState<number | null>(null);
  const [modelName, voiceName] = voice?.split(" · ") ?? [];

  const phase = narration?.phase ?? null;
  const positionSec = narration?.positionSec ?? 0;
  const playing = phase === "playing";
  const paused = phase === "paused";
  const finished = phase === "finished";
  const speeds = [...new Set([...PLAYBACK_SPEEDS, speed])].sort((left, right) => left - right);

  // The scrubber is a clock, not a table of contents: it runs the length of
  // the Narration's audio and the thumb sits at the playhead, so a second of
  // dragging is a second of audio wherever it happens. On a Narration still
  // being read the track is as long as what has been assembled so far and
  // grows behind the thumb; the Engine clamps a seek past the measured audio
  // to the end of it. Whole seconds, since the thumb steps by one: a
  // fractional length is a last second the thumb could never reach.
  const total = Math.max(Math.round(totalSec), 0);
  const at = dragging ?? Math.min(Math.max(Math.round(positionSec), 0), total);
  const seekable = onSeekTime !== null && total > 0;
  const [elapsed, length] = formatPlayhead(at, total);

  // Seeking can stop being possible mid-drag — another Narration starts, or
  // this one is stopped — and a disabled input does not reliably blur.
  // Letting the thumb go here means it is following the Engine again the
  // moment there is something to follow.
  if (dragging !== null && !seekable) setDragging(null);

  const [cancelling, setCancelling] = useState(false);
  if (cancelling && onCancel === null) setCancelling(false);
  const cancel = () => {
    if (cancelling || onCancel === null) return;
    setCancelling(true);
    void onCancel().then((stopped) => {
      if (!stopped) setCancelling(false);
    });
  };

  const commit = () => {
    if (dragging === null) return;
    onSeekTime?.(dragging);
    setDragging(null);
  };

  const canStepBack = onSeekTime !== null && positionSec > 0;
  const canStepForward = onSeekTime !== null && positionSec < total;
  const fill = total > 0 ? (at / total) * 100 : 0;

  return (
    <div className="player">
      <div className="player__track">
        <input
          aria-label="Position"
          // Read out in the clock's own words, so a screen reader hears the
          // same "0:30 of 1:02" a sighted reader sees beside the track.
          aria-valuetext={seekable ? `${elapsed} of ${length}` : "Nothing to play"}
          className="player__scrubber"
          disabled={!seekable}
          max={total}
          min={0}
          onBlur={commit}
          onChange={(event) => setDragging(Number(event.target.value))}
          onKeyUp={commit}
          // A cancelled pointer is a drag that never ended, and a thumb that
          // never lets go pins itself to a stale second for good — `at`
          // prefers `dragging` over the playhead, so it would stop tracking
          // the Engine entirely.
          onPointerCancel={commit}
          onPointerUp={commit}
          step={1}
          style={{ "--player-fill": `${fill}%` } as React.CSSProperties}
          type="range"
          value={at}
        />
        <span className="player__clock">
          <span>{elapsed}</span>
          <span className="visually-hidden"> of </span>
          <span>{length}</span>
        </span>
      </div>

      <div className="player__transport">
        <span className="player__lead">
          <span className="player__voice">
            <VoiceOrb {...orb} animated={playing} level={playing ? narration?.level ?? 0 : 0} size={20} />
            {voice !== null && (
              <>
                <span className="player__voice-name">{voiceName}</span>
                <span aria-hidden="true" className="player__voice-dot">·</span>
                <span className="player__voice-model">{modelName}</span>
              </>
            )}
          </span>
          {faster !== null && (
            <button className="player__faster" type="button" onClick={faster.onTake}>
              Getting impatient? Try {faster.name} instead
            </button>
          )}
        </span>

        <span className="player__centre">
          <button
            aria-label="Back 15 seconds"
            title="Back 15 seconds"
            className="player__skip"
            disabled={!canStepBack}
            onClick={() => onSeekTime?.(Math.max(0, positionSec - 15))}
            type="button"
          >
            <RotateCcw size={30} strokeWidth={1.6} />
            <span aria-hidden="true" className="player__skip-seconds">15</span>
          </button>
          <button
            aria-label={playing ? "Pause" : finished ? "Play again" : "Play"}
            className="player__toggle"
            disabled={!playing && !paused && !finished}
            onClick={playing ? onPause : onPlay}
            type="button"
          >
            {playing ? <Pause size={30} fill="currentColor" strokeWidth={0} /> : <Play size={30} fill="currentColor" strokeWidth={0} />}
          </button>
          <button
            aria-label="Forward 30 seconds"
            title="Forward 30 seconds"
            className="player__skip"
            disabled={!canStepForward}
            onClick={() => onSeekTime?.(Math.min(total, positionSec + 30))}
            type="button"
          >
            <RotateCw size={30} strokeWidth={1.6} />
            <span aria-hidden="true" className="player__skip-seconds">30</span>
          </button>
        </span>

        <span className="player__right">
          {onCancel !== null && (
            <button
              className="player__cancel"
              type="button"
              // `aria-disabled` rather than `disabled`, so focus stays on the
              // button while its name says the cancel is under way.
              aria-disabled={cancelling}
              onClick={cancel}
            >
              {cancelling ? "Cancelling…" : "Cancel"}
            </button>
          )}
          <select
            aria-label="Playback speed"
            className="player__speed"
            onChange={(event) => onSpeed(Number(event.target.value))}
            value={speed}
          >
            {speeds.map((choice) => (
              <option key={choice} value={choice}>{choice}×</option>
            ))}
          </select>
        </span>
      </div>

      <p aria-hidden="true" className="player__problem">
        {problem ?? ""}
      </p>

      <p aria-live="polite" className="player__generation">
        {narration?.generationBehind ? "Generation is falling behind at this speed. Pause to let it catch up, or turn “Prepare the whole Narration first” back on in Controls before starting a new Narration." : ""}
      </p>

      {/* Announced, like every other notice the shell raises: an Export that
       * failed is a thing the reader asked for and did not get. It is a
       * different sentence from the Narration's own, which is announced from
       * its own region below, so the two can stand together. Mounted with nothing to say,
       * for the reason `NarrationStatus` is: a region that arrives already
       * holding its first message is announced unreliably. */}
      <p aria-live="polite" className="player__notice">
        {notice ?? ""}
      </p>
    </div>
  );
}
