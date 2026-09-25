import { useEffect, useId, useRef, useState } from "react";
import type { CatalogEntry, ModelStatus, VoiceSelection } from "../engine/client";
import { Check, ChevronDown, Download } from "lucide-react";
import { voiceToOffer } from "./lastVoice";

export type ModelMenuProps = {
  // Every Catalog entry, or `null` before the Catalog has been read.
  entries: CatalogEntry[] | null;
  statusOf: (entryId: string) => ModelStatus | undefined;
  // The Voice the composer narrates with, or `null` before the Engine has
  // said which it is.
  selection: VoiceSelection | null;
  onSelect: (chosen: VoiceSelection) => void;
  // Opens the Catalog sheet, which is where a Voice Model is downloaded,
  // read about, and deleted.
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
// Manifest's pick when no remembered Voice is on offer. A model that is not on
// disk is listed too, with a download mark, and choosing it opens the
// Catalog sheet: acquiring a Voice Model is a deliberate detour, not a
// thing to trip into from a menu.
export default function ModelMenu({
  entries,
  statusOf,
  selection,
  onSelect,
  onBrowse,
}: ModelMenuProps) {
  const [open, setOpen] = useState(false);
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

  return (
    <div
      className="model-menu"
      ref={region}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) close();
      }}
    >
      <button
        aria-controls={open ? menuId : undefined}
        aria-expanded={open}
        aria-label={current === null ? "Voice Model" : `Voice Model: ${current.name}`}
        className="model-menu__pill"
        onClick={() => setOpen(!open)}
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
            const missing = statusOf(entry.id)?.installed === false;
            const chosen = entry.id === selection?.modelId;
            return (
              <li key={entry.id}>
                <button
                  aria-current={chosen ? "true" : undefined}
                  className={`model-menu__row${chosen ? " model-menu__row--chosen" : ""}`}
                  onClick={() => {
                    if (missing) {
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
                  <span className="model-menu__label">
                    {entry.name}
                    <span className={tierClass(entry.tier)}>{entry.tier}</span>
                  </span>
                  <span className="model-menu__marks">
                    {missing && (
                      <span className="model-menu__download" title="Not downloaded">
                        <Download size={14} />
                        <span className="visually-hidden">Not downloaded</span>
                      </span>
                    )}
                    {chosen && <Check size={14} />}
                  </span>
                </button>
              </li>
            );
          })}
          <li>
            <button
              className="model-menu__row model-menu__row--browse"
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
