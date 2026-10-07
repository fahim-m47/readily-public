import { useEffect, useRef, useState } from "react";
import type { EngineBinding } from "../engine/useEngine";
import { firstRunStep } from "./firstrun";
import type { FirstRunStep } from "./firstrun";
import { acceptLicences, acceptedLicences, licencesOf } from "./terms";
import type { Licence } from "./terms";
import type { CatalogBinding } from "./useCatalog";
import type { DownloadBinding } from "./useDownloads";

export type FirstRunBinding = {
  // The wait to show instead of the shell, or `null` whenever the app is
  // the reader's to use.
  step: FirstRunStep | null;
  // What the step's one button does, or `null` when nothing is stuck.
  onRetry: (() => void) | null;
  // Every licence in the Catalog and the one tick that accepts them all,
  // while the step is asking for it.
  terms: { licences: Licence[]; accept: () => void } | null;
};

// Whether a first run is still happening, and the two things it does about
// it: fetch the first Voice Model, and offer a way back out of a failure.
//
// The screen this drives takes over from the shell from the first frame
// until the app is usable: the Engine ready, the licences accepted, and a
// Voice Model on disk. It never comes back within a session, with one
// exception: licences a Catalog that failed at launch brings once it is
// read. A reader with a model on disk is let in past that failure, and the
// licence ask returns before anything new can be downloaded. If the reader
// deletes their last model, that is the Catalog sheet's business, and a
// takeover would be the app changing its mind about what it is; an Engine
// restart mid-session is the sidebar's line to report.
//
// That latch is React state, so it lasts exactly as long as the window. A
// reader who deletes their last Voice Model and relaunches is
// indistinguishable here from one whose first run never finished, and gets
// the takeover — and its download — again. Telling those two apart needs a
// durable "this machine has been set up" fact, which nothing on the wire
// carries today.
export const useFirstRun = (
  engine: EngineBinding,
  catalog: CatalogBinding,
  downloads: DownloadBinding,
): FirstRunBinding => {
  const { entries, defaultModelId, voiceModelInstalled } = catalog;
  const { start } = downloads;

  // The Voice Model a first run lands: the Manifest's own default, and the
  // first entry if the Catalog ever names a default it does not carry.
  const firstVoiceModel =
    entries?.find((entry) => entry.id === defaultModelId) ?? entries?.[0] ?? null;

  const licences = entries === null ? null : licencesOf(entries);
  const [accepted, setAccepted] = useState(acceptedLicences);

  const step = firstRunStep({
    connection: engine.connection,
    anyVoiceModel: voiceModelInstalled(null),
    firstVoiceModel,
    // The Catalog read comes first because it is the one that leaves the
    // screen with nothing to name; either way both are the same kind of
    // stuck and the same button clears them.
    storeFailure: catalog.failure ?? catalog.notice,
    unaccepted: licences?.filter((licence) => !accepted.includes(licence.terms.id)) ?? null,
    download: downloads.download,
    downloadFailure: downloads.notice,
  });

  // Set during render rather than in an effect, on purpose: an effect
  // would commit one frame of the wrong screen first, and the wrong screen
  // here is the launch screen blinking back over the app.
  const [handedOff, setHandedOff] = useState(false);
  if (step === null && !handedOff) setHandedOff(true);

  const showing = handedOff && step?.kind !== "terms" ? null : step;

  // The first Voice Model is fetched without being asked for. A first run
  // has no meaningful choice in it — an app that cannot speak is not an app
  // — and making a reader find the Catalog to make the only choice
  // available would be ceremony. Every *later* download is still theirs to
  // start.
  //
  // Once per model, and never after a failure: a screen that re-asked on
  // its own would turn a dead network into an invisible retry loop, and the
  // reader would be watching a number that never moves with nothing to
  // press. The failure has a button; this does not press it.
  const wanted =
    showing?.kind === "getting-voice-model" ? (firstVoiceModel?.id ?? null) : null;
  const asked = useRef<string | null>(null);
  useEffect(() => {
    if (wanted === null || asked.current === wanted) return;
    asked.current = wanted;
    start(wanted);
  }, [start, wanted]);

  // Which button the step asked for, resolved to the thing it does. Held as
  // the callback rather than as a name the component would have to switch
  // on again: a screen cannot render a retry there is no retry for.
  const model = firstVoiceModel;
  const onRetry =
    showing?.retry === "engine"
      ? engine.retry
      : showing?.retry === "store"
        ? () => void catalog.refresh()
        : showing?.retry === "download" && model !== null
          ? () => start(model.id)
          : null;

  const terms =
    showing?.kind === "terms" && licences !== null
      ? {
          licences,
          accept: () => {
            const ids = licences.map((licence) => licence.terms.id);
            acceptLicences(ids);
            setAccepted((before) => [...before, ...ids]);
          },
        }
      : null;

  return { step: showing, onRetry, terms };
};
