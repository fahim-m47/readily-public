import { useEffect, useId, useRef, useState } from "react";
import type { CatalogEntry, ModelStatus, VoiceSelection } from "../engine/client";
import { ChevronDown, Clock, Download, LoaderCircle, Trash2 } from "lucide-react";
import { UNRUNNABLE_REASON, describeRowDownload } from "./catalog";
import { voiceToOffer } from "./lastVoice";
import type { DownloadBinding } from "./useDownloads";

export type ModelMenuProps = {
  // Every Catalog entry, or `null` before the Catalog has been read.
  entries: CatalogEntry[] | null;
  statusOf: (entryId: string) => ModelStatus | undefined;
  // The Engine's download queue, shared with the Catalog sheet so a
  // download started here shows its progress there too.
  downloads: DownloadBinding;
  // The Voice the composer narrates with, or `null` before the Engine has
  // said which it is.
  selection: VoiceSelection | null;
  onSelect: (chosen: VoiceSelection) => void;
  // Opens the Catalog sheet, which is where a Voice Model is auditioned and
  // read about before it is downloaded.
  onBrowse: () => void;
};

// A Tier's badge class. Tier is the Manifest's own word (CONTEXT.md); the
// two the Catalog uses today each get a colour, anything else the neutral.
const tierClass = (tier: string) =>
  `tier tier--${/instant/i.test(tier) ? "instant" : /expressive/i.test(tier) ? "expressive" : "other"}`;

// The composer's Voice Model pill: which model is speaking, and a menu of
// the whole Catalog one click away.
//
// Picking an installed model keeps the chosen Voice when it belongs to that
// model, otherwise the Voice the reader last left that model on, and the
// Manifest's pick when no remembered Voice is on offer. Picking a model that
// is not on disk opens the Catalog sheet, so its Voices can be heard first;
// each row's trailing button downloads it, or deletes it once it is on disk.
export default function ModelMenu({
  entries,
  statusOf,
  downloads,
  selection,
  onSelect,
  onBrowse,
}: ModelMenuProps) {
  const [open, setOpen] = useState(false);
  // The row whose trash button has been pressed once. Deleting is
  // recoverable, but a re-download is hundreds of megabytes, so it arms
  // first, the same way the Catalog sheet's button does.
  const [armed, setArmed] = useState<string | null>(null);
  const region = useRef<HTMLDivElement>(null);
  const pill = useRef<HTMLButtonElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      if (!region.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);

  const close = () => {
    setOpen(false);
    pill.current?.focus();
  };

  const current = entries?.find((entry) => entry.id === selection?.modelId) ?? null;
  // The sheet is closed in this flow, so a failure has to be said here or
  // not at all: the reader's last refused request, and the Engine's latest
  // failed download.
  const failedId = downloads.download?.failures.at(-1)?.modelId ?? null;
  const failedLine =
    failedId === null ? null : describeRowDownload(failedId, downloads.download).line;
  const failedName = entries?.find((entry) => entry.id === failedId)?.name;

  return (
    <div
      className="model-menu"
      ref={region}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        if (armed !== null) setArmed(null);
        else if (open) close();
      }}
    >
      <button
        aria-controls={open ? menuId : undefined}
        aria-expanded={open}
        aria-label={current === null ? "Voice Model" : `Voice Model: ${current.name}`}
        className="model-menu__pill"
        onClick={() => {
          // Every way the popover closes passes through here before it can
          // reopen, so a trash button never comes back already armed.
          setArmed(null);
          setOpen(!open);
        }}
        ref={pill}
        type="button"
      >
        <span className="model-menu__name">{current?.name ?? "Voice models"}</span>
        {current !== null && <span className={tierClass(current.tier)}>{current.tier}</span>}
        <ChevronDown size={12} />
      </button>

      {open && (
        <ul className="model-menu__list" id={menuId} role="group" aria-label="Voice Models">
          {(entries ?? []).map((entry) => {
            const status = statusOf(entry.id);
            const installed = status?.installed;
            const chosen = entry.id === selection?.modelId;
            const { inFlight, queued } = describeRowDownload(entry.id, downloads.download);
            const isArmed = armed === entry.id;
            // Still opens the sheet, where its Voices can be heard, but is
            // never picked and offers no download: the Engine would refuse
            // both. One already on disk keeps only its delete button.
            const unrunnable = !entry.runsHere;
            return (
              <li
                className={`model-menu__row${chosen ? " model-menu__row--chosen" : ""}${unrunnable ? " model-menu__row--unrunnable" : ""}`}
                key={entry.id}
              >
                <button
                  aria-current={chosen ? "true" : undefined}
                  className="model-menu__pick"
                  onClick={() => {
                    if (unrunnable || installed === false) {
                      setOpen(false);
                      onBrowse();
                      return;
                    }
                    const offered = (chosen
                      ? entry.voices.find((voice) => voice.id === selection?.voiceId)
                      : undefined) ?? voiceToOffer(entry);
                    if (offered) onSelect({ modelId: entry.id, voiceId: offered.id });
                    close();
                  }}
                  type="button"
                >
                  {entry.name}
                  <span className={tierClass(entry.tier)}>{entry.tier}</span>
                  {unrunnable && <span className="model-menu__note">{UNRUNNABLE_REASON}</span>}
                </button>

                {unrunnable && installed !== true ? null : queued !== null ? (
                  <button
                    aria-label={`Remove ${entry.name} from the queue`}
                    className="model-menu__action"
                    onClick={() => downloads.withdraw(entry.id)}
                    title={`Waiting to ${queued} — click to remove from the queue`}
                    type="button"
                  >
                    <Clock size={14} />
                  </button>
                ) : inFlight || installed === false ? (
                  // One button from Download through to done, so keyboard
                  // focus stays on the row while the spinner turns: a
                  // `disabled` button would drop focus, `aria-disabled` keeps it.
                  <button
                    aria-disabled={inFlight || undefined}
                    aria-label={inFlight ? `Downloading ${entry.name}` : `Download ${entry.name}`}
                    className="model-menu__action"
                    onClick={() => {
                      if (!inFlight) downloads.start(entry.id);
                    }}
                    title={inFlight ? "Downloading" : "Download"}
                    type="button"
                  >
                    {inFlight ? (
                      <LoaderCircle aria-hidden="true" className="model-menu__spinner" size={14} />
                    ) : (
                      <Download size={14} />
                    )}
                  </button>
                ) : installed === true ? (
                  <button
                    aria-label={isArmed ? `Confirm delete ${entry.name}` : `Delete ${entry.name}`}
                    className={`model-menu__action model-menu__action--delete${isArmed ? " model-menu__action--armed" : ""}`}
                    onBlur={() => setArmed(null)}
                    onClick={() => {
                      if (!isArmed) {
                        setArmed(entry.id);
                        return;
                      }
                      setArmed(null);
                      downloads.remove(entry.id, entry.name);
                    }}
                    title={isArmed ? "Click again to delete" : "Delete"}
                    type="button"
                  >
                    <Trash2 size={14} />
                  </button>
                ) : null}
              </li>
            );
          })}
          {(downloads.notice !== null || failedLine !== null) && (
            <li className="model-menu__notice" role="status">
              {downloads.notice ?? `${failedName ?? "Download"}: ${failedLine?.message}`}
            </li>
          )}
          <li>
            <button
              className="model-menu__browse"
              onClick={() => {
                setOpen(false);
                onBrowse();
              }}
              type="button"
            >
              Manage voice models…
            </button>
          </li>
        </ul>
      )}
    </div>
  );
}
