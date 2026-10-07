import { useCallback, useEffect, useRef, useState } from "react";
import type { EngineClient, UpdateStatus } from "../engine/client";

// The offer to put in front of the reader, and how it is going.
export type UpdateOffer = {
  // The published version, as `latest.json` names it.
  version: string;
  // The release's own prose, or `null` when the release carried none.
  notes: string | null;
  // Whether this copy can install it. False on Linux, where the reader is
  // only told and sent to the download page.
  installable: boolean;
  // The reader said yes and the download is under way. Readily restarts
  // itself when it lands, so there is no "done" — the window goes away.
  installing: boolean;
  // Why the install did not happen. `untouched` is whether this build is
  // still the one on disk: a download that failed left it alone and the
  // offer can be taken again; a swap that failed may have left nothing to
  // relaunch, and the honest advice is a fresh download.
  failure: { reason: string; untouched: boolean } | null;
};

export type UpdateBinding = {
  // `null` whenever there is nothing to ask the reader about: no newer
  // Readily published, the check could not be made, or they chose Later.
  offer: UpdateOffer | null;
  // Take the update. Only the button's label moves on the shell's say-so;
  // everything else is the supervisor's answer, read by the poll below.
  install: () => void;
  // Send the reader to the download page, for a release they cannot install
  // from here.
  openDownloadPage: () => void;
  // Not now. Gone for this run of the app; the next launch asks again.
  dismiss: () => void;
};

// How often the shell re-reads the update status. The supervisor answers
// it from memory — the network check happens once, on its own, seconds
// after launch — so this is a cheap question asked slowly, only so the
// offer appears without the reader having to reopen anything.
const POLL_MS = 10_000;

// Binds a screen to the shell's update state.
//
// Polls, like everything else the Rust side reports: the shell subscribes
// to nothing, so a screen mounted after the check has already answered
// still sees the offer. The reader's answer is the only thing that starts
// an install — the supervisor never installs on its own.
export const useUpdate = (client: EngineClient): UpdateBinding => {
  // What was published. Sticky once it arrives, because the installing and
  // failed statuses do not carry it: a prompt that lost the version it was
  // about halfway through the install would be a worse thing to show than
  // one that keeps naming the release it is installing.
  const [offered, setOffered] = useState<Pick<
    UpdateOffer,
    "version" | "notes" | "installable"
  > | null>(null);
  const [phase, setPhase] = useState<UpdateStatus["state"]>("idle");
  const [failure, setFailure] = useState<UpdateOffer["failure"]>(null);
  const [dismissed, setDismissed] = useState(false);
  // Set the moment the reader says yes, and never cleared. A status read
  // that was already in flight when they pressed Install will answer
  // `available` — the question was asked before the install existed — and
  // letting that land would put the button back to "Install".
  const asked = useRef(false);

  useEffect(() => {
    let live = true;
    let timer = 0;

    const take = (status: UpdateStatus) => {
      if (status.state === "available" || status.state === "announced") {
        // Same object while the published version has not moved, so a poll
        // every ten seconds is not a re-render every ten seconds.
        setOffered((current) =>
          current?.version === status.version
            ? current
            : {
                version: status.version,
                notes: status.notes,
                installable: status.state === "available",
              },
        );
      }
      if (status.state === "failed") {
        setFailure({ reason: status.reason, untouched: status.untouched });
      }
      if (status.state === "available" && asked.current) return;
      setPhase(status.state);
    };

    const poll = async () => {
      try {
        const status = await client.updateStatus();
        if (live) take(status);
      } catch {
        // The supervisor answers this from its own memory, so a failure
        // here is the bridge rather than the network — and an update the
        // reader never asked about is not worth a sentence either way.
      }
      if (live) timer = window.setTimeout(() => void poll(), POLL_MS);
    };

    void poll();

    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [client]);

  const install = useCallback(() => {
    asked.current = true;
    // The supervisor refuses a second install itself, so this is only the
    // label: the button says "Installing…" now rather than in ten seconds.
    setFailure(null);
    setPhase("installing");
    void client.installUpdate().catch(() => {
      setFailure({ reason: "The update could not be started.", untouched: true });
      setPhase("failed");
    });
  }, [client]);

  const openDownloadPage = useCallback(() => {
    // The page still names the address, so a browser that did not open
    // leaves the reader something to type.
    void client.openDownloadPage().catch(() => {});
  }, [client]);

  const dismiss = useCallback(() => setDismissed(true), []);

  const offer =
    dismissed || offered === null
      ? null
      : {
          ...offered,
          installing: phase === "installing",
          failure: phase === "failed" ? failure : null,
        };

  return { offer, install, openDownloadPage, dismiss };
};
