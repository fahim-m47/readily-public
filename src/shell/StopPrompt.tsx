import { useEffect, useRef, useState } from "react";

export type StopPromptProps = {
  // The first words of the Narration the Engine is still reading.
  playing: string;
  // The Narration the question is about has ended on its own, so there is
  // nothing left to stop.
  moot: boolean;
  // The question answered itself: the row the reader asked for can open
  // without stopping anything. Followed by `onKeep`, as every close is.
  onMoot: () => void;
  // Settles once the Engine has answered the stop, either way.
  onStop: () => Promise<unknown>;
  onKeep: () => void;
};

// Asked when a reader opens a History row while another Narration is being
// read. The Engine reads one Narration at a time, so opening the row means
// stopping the other one. Until there is a miniplayer that keeps playing
// while the reader moves around, the reader chooses.
export default function StopPrompt({
  playing,
  moot,
  onMoot,
  onStop,
  onKeep,
}: StopPromptProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
  }, []);

  // Closing the dialog, rather than unmounting it, is what hands focus back
  // to the row that raised it. Either answer ends by closing, and `onClose`
  // reports that the question is over, whichever way it went.
  const close = () => dialog.current?.close();

  // A stop already under way opens the row on its own once it settles, so
  // the question is only moot while nobody has answered it. Answered once:
  // the `close` event that unmounts this arrives a task later, and a
  // render in between must not open the row again.
  const answered = useRef(false);
  useEffect(() => {
    if (!moot || stopping || answered.current) return;
    answered.current = true;
    onMoot();
    close();
  }, [moot, onMoot, stopping]);

  return (
    <dialog
      className="sheet sheet--prompt"
      ref={dialog}
      aria-labelledby="stop-prompt-title"
      onClose={onKeep}
      onCancel={(event) => {
        if (stopping) event.preventDefault();
      }}
    >
      <div className="sheet__body">
        <h2 className="sheet__title" id="stop-prompt-title">
          Still reading
        </h2>
        <p className="sheet__lede">
          Readily is reading “{playing}”. Opening this one stops it.
        </p>
        <p className="visually-hidden" aria-live="polite">
          {stopping ? "Stopping the Narration…" : ""}
        </p>
        <div className="sheet__actions">
          <button
            className="sheet__close"
            type="button"
            disabled={stopping}
            onClick={close}
          >
            Keep listening
          </button>
          <button
            className="sheet__stop"
            type="button"
            // `aria-disabled` rather than `disabled`: the only other control
            // is disabled too, and a disabled button does not reliably keep
            // focus, which in a modal with nothing else to focus leaves the
            // reader nowhere. Its name changing under focus is what says
            // the stop is under way.
            aria-disabled={stopping}
            onClick={() => {
              if (stopping) return;
              setStopping(true);
              void onStop().finally(close);
            }}
          >
            {stopping ? "Stopping…" : "Stop and open"}
          </button>
        </div>
      </div>
    </dialog>
  );
}
