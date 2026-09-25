import { useEffect, useId, useRef, useState } from "react";
import type { Mode } from "../engine/client";
import { Check, ChevronDown } from "lucide-react";

const modes = ["simple", "advanced"] as const;
const labels = { simple: "Simple", advanced: "Advanced" };

// Keep mouse-down from blurring the selected option in WebKit: selection
// and focus restoration happen on click, before the choices are removed.
export default function ModeMenu({ mode, disabled, onChange }: {
  mode: Mode;
  disabled: boolean;
  onChange: (mode: Mode) => void;
}) {
  const [open, setOpen] = useState(false);
  const region = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const choices = useRef<HTMLDivElement>(null);
  const id = useId();

  if (disabled && open) setOpen(false);

  useEffect(() => {
    if (!open) return;
    choices.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus();
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !region.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);

  const close = () => {
    setOpen(false);
    trigger.current?.focus();
  };

  return (
    <div className="mode" ref={region}
      onMouseDown={(event) => event.preventDefault()}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.preventDefault();
          close();
        }
      }}
    >
      <button className="mode__trigger" type="button" ref={trigger}
        aria-label={`Reading mode: ${labels[mode]}`} aria-expanded={open}
        aria-controls={open ? id : undefined} disabled={disabled}
        onClick={() => open ? close() : setOpen(true)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        {labels[mode]} <ChevronDown size={12} />
      </button>
      {open && (
        <div className="mode__choices" id={id} role="group" aria-label="Reading mode" ref={choices}
          onKeyDown={(event) => {
            if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
            event.preventDefault();
            const buttons = Array.from(event.currentTarget.querySelectorAll("button"));
            const index = buttons.findIndex((button) => button === document.activeElement);
            const step = event.key === "ArrowDown" ? 1 : -1;
            buttons[(index + step + buttons.length) % buttons.length]?.focus();
          }}
        >
          {modes.map((choice) => (
            <button className="mode__choice" type="button" key={choice} aria-pressed={choice === mode}
              onClick={() => { onChange(choice); close(); }}
            >
              {labels[choice]}
              {choice === mode && <Check size={12} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
