import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import StopPrompt from "./StopPrompt";

afterEach(cleanup);

const mount = () => {
  let settle = () => {};
  const onStop = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        settle = resolve;
      }),
  );
  const onKeep = vi.fn();
  const onMoot = vi.fn();
  const prompt = (moot: boolean) => (
    <StopPrompt generating="The sea" moot={moot} onMoot={() => onMoot()} onStop={onStop} onKeep={onKeep} />
  );
  const { rerender } = render(prompt(false));
  const dialog = screen.getByRole("dialog", { name: "Still generating" }) as HTMLDialogElement;
  expect(dialog.open).toBe(true);
  return {
    dialog,
    onStop,
    onKeep,
    onMoot,
    settle: () => settle(),
    end: () => rerender(prompt(true)),
  };
};

test("the prompt names what is generating and what continuing does to it", () => {
  mount();
  expect(
    screen.getByText("Generating “The sea” is still in progress. Continuing will cancel it."),
  ).toBeTruthy();
});

test("a question that answers itself continues and closes", () => {
  const { dialog, onStop, onKeep, onMoot, end } = mount();
  end();
  expect(onMoot).toHaveBeenCalledOnce();
  expect(dialog.open).toBe(false);
  expect(onKeep).toHaveBeenCalledOnce();
  expect(onStop).not.toHaveBeenCalled();
});

test("a question answers itself once, however long the close takes", async () => {
  // A real dialog fires `close` a task after `close()`; the shell's polyfill
  // fires it inline. Renders in that gap must not continue again.
  const inline = HTMLDialogElement.prototype.close;
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    setTimeout(() => inline.call(this), 0);
  };
  try {
    const { dialog, onMoot, onKeep, end } = mount();
    end();
    end();
    expect(onMoot).toHaveBeenCalledOnce();
    expect(onKeep).not.toHaveBeenCalled();

    await waitFor(() => expect(dialog.open).toBe(false));
    expect(onMoot).toHaveBeenCalledOnce();
    expect(onKeep).toHaveBeenCalledOnce();
  } finally {
    HTMLDialogElement.prototype.close = inline;
  }
});

test("a stop under way is left alone", async () => {
  const { dialog, onStop, onKeep, onMoot, end, settle } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));
  end();
  expect(onMoot).not.toHaveBeenCalled();
  expect(dialog.open).toBe(true);

  settle();
  await waitFor(() => expect(dialog.open).toBe(false));
  expect(onMoot).not.toHaveBeenCalled();
  expect(onStop).toHaveBeenCalledOnce();
  expect(onKeep).toHaveBeenCalledOnce();
});

test("Keep generating closes the dialog and only that", () => {
  const { dialog, onStop, onKeep } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Keep generating" }));
  expect(dialog.open).toBe(false);
  expect(onKeep).toHaveBeenCalledOnce();
  expect(onStop).not.toHaveBeenCalled();
});

test("Cancel generation and continue holds the dialog until the stop settles, then closes it", async () => {
  const { dialog, onStop, onKeep, settle } = mount();
  fireEvent.click(screen.getByRole("button", { name: "Cancel generation and continue" }));
  expect(onStop).toHaveBeenCalledOnce();
  expect(dialog.open).toBe(true);
  expect(onKeep).not.toHaveBeenCalled();
  const stopping = screen.getByRole("button", { name: "Cancelling…" }) as HTMLButtonElement;
  expect(stopping.getAttribute("aria-disabled")).toBe("true");
  expect(stopping.disabled).toBe(false);
  expect(screen.getByText("Cancelling the Narration…").getAttribute("aria-live")).toBe("polite");
  fireEvent.click(stopping);
  expect(onStop).toHaveBeenCalledOnce();
  expect((screen.getByRole("button", { name: "Keep generating" }) as HTMLButtonElement).disabled).toBe(true);
  expect(dialog.dispatchEvent(new Event("cancel", { cancelable: true }))).toBe(false);

  settle();
  await waitFor(() => expect(dialog.open).toBe(false));
  expect(onKeep).toHaveBeenCalledOnce();
});

test("Escape counts as Keep generating, so the reader can ask again", () => {
  const { dialog, onStop, onKeep } = mount();
  expect(dialog.dispatchEvent(new Event("cancel", { cancelable: true }))).toBe(true);
  dialog.close();
  expect(onKeep).toHaveBeenCalledOnce();
  expect(onStop).not.toHaveBeenCalled();
});
