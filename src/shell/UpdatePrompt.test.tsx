import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import UpdatePrompt from "./UpdatePrompt";
import type { UpdateOffer } from "./useUpdate";

afterEach(cleanup);

const OFFER: UpdateOffer = {
  version: "0.2.0",
  notes: "Faster Narration.",
  installable: true,
  installing: false,
  failure: null,
};

const mount = (offer: Partial<UpdateOffer> = {}) => {
  const onInstall = vi.fn();
  const onDownload = vi.fn();
  const onLater = vi.fn();
  const props = (over: Partial<UpdateOffer>) => (
    <UpdatePrompt
      offer={{ ...OFFER, ...over }}
      onInstall={onInstall}
      onDownload={onDownload}
      onLater={onLater}
    />
  );
  const { rerender } = render(props(offer));
  const dialog = screen.getByRole("dialog", {
    name: /^Readily 0\.2\.0 is /,
  }) as HTMLDialogElement;
  expect(dialog.open).toBe(true);
  return {
    dialog,
    onInstall,
    onDownload,
    onLater,
    show: (over: Partial<UpdateOffer>) => rerender(props({ ...offer, ...over })),
  };
};

test("the reader is told which version and what changed", () => {
  mount();
  expect(screen.getByText("Faster Narration.")).toBeTruthy();
});

test("a release with no notes still says what taking it does", () => {
  mount({ notes: null });
  expect(screen.getByText(/Readily restarts itself/)).toBeTruthy();
});

test("a changelog pasted into a release cannot push the buttons off the screen", () => {
  mount({ notes: "x".repeat(5000) });
  const shown = screen.getByText(/^x+$/).textContent ?? "";
  expect(shown.length).toBe(600);
  expect(screen.getByRole("button", { name: "Install and restart" })).toBeTruthy();
});

test("nothing installs until the reader says so", () => {
  const { onInstall, onLater } = mount();
  expect(onInstall).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Install and restart" }));
  expect(onInstall).toHaveBeenCalledOnce();
  expect(onLater).not.toHaveBeenCalled();
});

test("escape is the same as later while nothing is installing", () => {
  const { dialog, onLater } = mount();
  const escape = new Event("cancel", { cancelable: true });
  dialog.dispatchEvent(escape);
  expect(escape.defaultPrevented).toBe(false);
  // jsdom's shim does not close on `cancel` the way a real dialog does, so
  // this only states that the prompt did not refuse the reader.
  expect(onLater).not.toHaveBeenCalled();
});

// On Linux Readily is a package its package manager owns, so the shell
// never installs one: the reader is told, and sent where they got this one.
test("a release this copy cannot install sends the reader to the download page", () => {
  const { dialog, onInstall, onDownload, onLater } = mount({ installable: false });
  expect(screen.getByRole("heading", { name: "Readily 0.2.0 is out" })).toBeTruthy();
  expect(screen.getByText("Faster Narration.")).toBeTruthy();
  expect(screen.getByText(/readily-download\.vercel\.app/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Install and restart" })).toBe(null);

  fireEvent.click(screen.getByRole("button", { name: "Open download page" }));
  expect(onDownload).toHaveBeenCalledOnce();
  expect(onInstall).not.toHaveBeenCalled();
  // Still up, address and all: a browser that never appeared has not
  // taken the reader's one way to the download with it.
  expect(dialog.open).toBe(true);
  expect(screen.getByText(/readily-download\.vercel\.app/)).toBeTruthy();
  expect(onLater).not.toHaveBeenCalled();
});

test("later closes the question", () => {
  const { dialog, onInstall, onLater } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Later" }));
  expect(dialog.open).toBe(false);
  expect(onLater).toHaveBeenCalledOnce();
  expect(onInstall).not.toHaveBeenCalled();
});

test("an install under way cannot be started twice or walked away from", () => {
  const { dialog, onInstall, onLater, show } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Install and restart" }));
  show({ installing: true });

  const installing = screen.getByRole("button", { name: "Installing…" });
  expect(installing.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(installing);
  expect(onInstall).toHaveBeenCalledOnce();

  // Escape on a real modal raises `cancel`; jsdom's dialog shim has no
  // modality, so the event is raised directly. Refusing it is what keeps the
  // reader from walking away from an app being replaced on disk.
  const escape = new Event("cancel", { cancelable: true });
  dialog.dispatchEvent(escape);
  expect(escape.defaultPrevented).toBe(true);
  expect(dialog.open).toBe(true);
  expect(onLater).not.toHaveBeenCalled();
});

test("an install that failed says this copy is untouched and offers another go", () => {
  mount({ failure: { reason: "The download did not finish.", untouched: true } });
  expect(screen.getByRole("alert").textContent).toContain(
    "The download did not finish.",
  );
  expect(screen.getByRole("alert").textContent).toContain("untouched");
  expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
});

// The plugin moves the running bundle aside before the new one lands, so a
// swap that failed may have left nothing to relaunch. Offering "Try again"
// there would download into the same hole; the honest advice is a fresh copy.
test("a swap that failed does not pretend this copy is fine", () => {
  const { onLater } = mount({
    failure: { reason: "Failed to move the new app into place", untouched: false },
  });
  expect(screen.getByRole("alert").textContent).toContain("fresh copy");
  expect(screen.queryByRole("button", { name: "Try again" })).toBe(null);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(onLater).toHaveBeenCalledOnce();
});

// The check answers at no particular moment. A modal opening over a Source
// being typed would take focus, and the keystrokes in flight, with it.
test("the offer waits until the reader has stopped typing", async () => {
  const source = document.createElement("textarea");
  document.body.appendChild(source);
  source.focus();
  try {
    render(
      <UpdatePrompt
        offer={OFFER}
        onInstall={vi.fn()}
        onDownload={vi.fn()}
        onLater={vi.fn()}
      />,
    );
    const dialog = screen.getByRole("dialog", { hidden: true }) as HTMLDialogElement;
    expect(dialog.open).toBe(false);

    source.blur();
    await waitFor(() => expect(dialog.open).toBe(true));
  } finally {
    source.remove();
  }
});
