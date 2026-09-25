import { useEffect, useId, useRef, type ReactNode } from "react";

type SheetProps = {
  title: string;
  variant?: "licence";
  onClose: () => void;
  children: ReactNode;
};

export default function Sheet({
  title,
  variant,
  onClose,
  children,
}: SheetProps) {
  const labelId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const body = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
    // showModal() autofocuses the first focusable child, which paints a focus
    // ring on Done and reads as "Done is selected" the moment a sheet opens.
    // Park focus on the body instead: keyboard users still tab into the sheet
    // from the top, and screen readers still get the dialog's label.
    body.current?.focus();
  }, []);

  return (
    <dialog
      className={variant ? `sheet sheet--${variant}` : "sheet"}
      ref={dialog}
      aria-labelledby={labelId}
      onClose={onClose}
      onClick={(event) => {
        if (event.target === dialog.current) dialog.current?.close();
      }}
    >
      <div className="sheet__body" ref={body} tabIndex={-1}>
        <div className="sheet__top">
          <h2 className="sheet__title" id={labelId}>
            {title}
          </h2>
          <button
            className="sheet__close"
            type="button"
            onClick={() => dialog.current?.close()}
          >
            Done
          </button>
        </div>
        {children}
      </div>
    </dialog>
  );
}
