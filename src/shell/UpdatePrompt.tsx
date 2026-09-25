import { useEffect, useRef } from "react";
import type { UpdateOffer } from "./useUpdate";

export type UpdatePromptProps = {
  offer: UpdateOffer;
  onInstall: () => void;
  // Not now. The prompt is gone for this run; the next launch asks again.
  onLater: () => void;
};

// The notes come off the release endpoint, so their length is not this
// app's to assume. Rendered as text by React either way — there is no
// markup path here — but a release with a changelog pasted into it should
// not be able to push the buttons off the screen.
const NOTES_LIMIT = 600;

// One paragraph per line, blank lines dropped, so a release's own line
// breaks survive without the prose needing a Markdown renderer.
const paragraphsOf = (notes: string) =>
  notes
    .slice(0, NOTES_LIMIT)
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

// Whether the reader is in the middle of typing somewhere. A modal takes
// focus the instant it opens, and the check that finds an update answers at
// no particular moment, so an offer that opened over a Source being typed
// would take the keystrokes in flight with it.
const typing = () => {
  const focused = document.activeElement;
  return (
    focused instanceof HTMLTextAreaElement ||
    focused instanceof HTMLInputElement ||
    (focused instanceof HTMLElement && focused.isContentEditable)
  );
};

// Asked once, when the launch check finds a newer Readily published. The
// reader's yes is the only thing that installs anything: the shell checks
// on its own but never updates on its own, so a reader is never
// surprised by a restart.
export default function UpdatePrompt({ offer, onInstall, onLater }: UpdatePromptProps) {
  const dialog = useRef<HTMLDialogElement>(null);

  // Opens now, or the moment the reader's focus leaves whatever they were
  // typing into. `focusout` fires before focus lands anywhere else, so the
  // question is asked a tick later, when it has.
  useEffect(() => {
    const open = () => {
      if (!dialog.current?.open) dialog.current?.showModal();
    };
    if (!typing()) {
      open();
      return;
    }
    let timer = 0;
    const settled = () => {
      timer = window.setTimeout(() => {
        if (typing()) return;
        document.removeEventListener("focusout", settled);
        open();
      }, 0);
    };
    document.addEventListener("focusout", settled);
    return () => {
      document.removeEventListener("focusout", settled);
      window.clearTimeout(timer);
    };
  }, []);

  const notes = offer.notes === null ? [] : paragraphsOf(offer.notes);
  // A swap that failed may have moved this Readily aside already, and a
  // second download would land in the same hole: the only honest button
  // left is the one that closes the prompt.
  const damaged = offer.failure !== null && !offer.failure.untouched;

  return (
    <dialog
      className="sheet sheet--prompt"
      ref={dialog}
      aria-labelledby="update-prompt-title"
      onClose={onLater}
      onCancel={(event) => {
        // Escape cannot abandon an install: the app is being replaced on
        // disk, and the reader has nothing to go back to until it lands.
        if (offer.installing) event.preventDefault();
      }}
    >
      <div className="sheet__body">
        <h2 className="sheet__title" id="update-prompt-title">
          Readily {offer.version} is ready
        </h2>
        {notes.length > 0 ? (
          notes.map((line, index) => (
            <p className="sheet__lede" key={index}>
              {line}
            </p>
          ))
        ) : (
          <p className="sheet__lede">
            Installing takes a moment, and Readily restarts itself when it is done.
          </p>
        )}
        {offer.failure !== null && (
          <p className="sheet__notice" role="alert">
            {offer.failure.reason}{" "}
            {offer.failure.untouched
              ? "This copy of Readily is untouched — you can try again, or keep using it."
              : "This copy of Readily may not open again. Download a fresh copy from the website before you quit."}
          </p>
        )}
        <p className="visually-hidden" aria-live="polite">
          {offer.installing ? "Installing the update…" : ""}
        </p>
        <div className="sheet__actions">
          <button
            className="sheet__close"
            type="button"
            disabled={offer.installing}
            onClick={() => dialog.current?.close()}
          >
            {damaged ? "Close" : "Later"}
          </button>
          {!damaged && (
            <button
              className="sheet__go"
              type="button"
              // `aria-disabled` rather than `disabled`, as in the stop
              // prompt: the only other control is disabled too, and a
              // disabled button does not reliably keep focus, which in a
              // modal with nothing else to focus leaves the reader nowhere.
              aria-disabled={offer.installing}
              onClick={() => {
                if (offer.installing) return;
                onInstall();
              }}
            >
              {offer.installing
                ? "Installing…"
                : offer.failure === null
                ? "Install and restart"
                : "Try again"}
            </button>
          )}
        </div>
      </div>
    </dialog>
  );
}
