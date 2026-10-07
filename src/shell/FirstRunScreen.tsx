import { Fragment, useId, useState } from "react";
import { LoaderCircle } from "lucide-react";
import type { FirstRunStep } from "./firstrun";
import LicenceView from "./LicenceView";
import type { Licence } from "./terms";

export type FirstRunScreenProps = {
  step: FirstRunStep;
  // The one way out of a stuck first run, or `null` when nothing is stuck
  // — the same posture as every other control in this shell: a button that
  // is on screen is one that does something.
  onRetry: (() => void) | null;
  // The licence beat's one tick, and each licence's text one tap away.
  terms: { licences: Licence[]; accept: () => void } | null;
};

// The first run, which is the app's first impression and a multi-minute
// one: the `.app` ships no Python and no Voice Model, so a clean machine
// has an environment to build and a Voice Model to fetch before anything
// can be read aloud (ADR 0001 §6). How much to fetch is the Catalog entry's
// `downloadBytes`, which `firstRunStep` puts in the detail line; no figure
// is written here, because one was, and it rotted.
//
// It takes the whole window rather than sitting in the shell. There is no
// shell to sit in yet — no History, no Catalog, and a composer whose
// Narrate button could only refuse — and a disabled app with a progress
// bar in the corner of it invites a reader to try things that cannot work.
// One screen, one sentence, one thing to wait for.
export default function FirstRunScreen({ step, onRetry, terms }: FirstRunScreenProps) {
  const [reading, setReading] = useState<Licence | null>(null);
  const licencesId = useId();

  return (
    <main className="firstrun" aria-label="Setting up Readily">
      <div className={`firstrun__card firstrun__card--${step.tone}`}>
        {/* The live region holds the headline alone. The detail under it
          * changes several times a second while bytes arrive, and a reader
          * on a screen reader would be read a new byte count over and over
          * instead of being told what is happening. */}
        <p className="firstrun__line" aria-live="polite">
          {step.tone === "working" && (
            <LoaderCircle aria-hidden="true" className="firstrun__spinner" />
          )}
          {step.message}
        </p>

        {step.detail !== null && (
          <p className="firstrun__detail">{step.detail}</p>
        )}

        {/* The machine's own words, deliberately quieter than the two
          * sentences above: `uv`'s latest line while the environment
          * builds, and the supervisor's reason once it has given up.
          * Neither is plain language, and neither should be thrown away —
          * one is the only proof a silent build is moving, the other is
          * the only thing a reader can quote when they ask for help. */}
        {step.aside !== null && <p className="firstrun__aside">{step.aside}</p>}

        {step.progress !== null && (
          <progress
            className="firstrun__bar"
            aria-label="Download progress"
            max={step.progress.total}
            value={step.progress.done}
          />
        )}

        {terms !== null && (
          <>
            <label className="firstrun__accept">
              <input aria-describedby={licencesId} type="checkbox" onChange={terms.accept} />
              I accept the terms and conditions of every voice model Readily supports
            </label>
            <p className="firstrun__licences" id={licencesId}>
              {terms.licences.map((licence, index) => (
                <Fragment key={licence.terms.id}>
                  {index > 0 && " · "}
                  <button
                    className="firstrun__licence"
                    onClick={() => setReading(licence)}
                    type="button"
                  >
                    {licence.terms.name}
                  </button>
                </Fragment>
              ))}
            </p>
          </>
        )}

        {onRetry !== null && (
          <button className="firstrun__retry" type="button" onClick={onRetry}>
            Try again
          </button>
        )}
      </div>

      {reading !== null && (
        <LicenceView
          entryName={reading.models.join(", ")}
          terms={reading.terms}
          onClose={() => setReading(null)}
        />
      )}
    </main>
  );
}
