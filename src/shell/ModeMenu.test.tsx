import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import ModeMenu from "./ModeMenu";

afterEach(cleanup);

test("a WebKit mouse click can switch modes before blur dismisses the choices", () => {
  const onChange = vi.fn();
  render(<ModeMenu mode="simple" disabled={false} onChange={onChange} />);
  fireEvent.click(screen.getByRole("button", { name: "Reading mode: Simple" }));
  const simple = screen.getByRole("button", { name: "Simple" });
  const advanced = screen.getByRole("button", { name: "Advanced" });
  // WebKit's default mouse action blurs the old button without focusing
  // the clicked button. The click only arrives after that focus change.
  if (fireEvent.mouseDown(advanced)) fireEvent.blur(simple, { relatedTarget: null });
  fireEvent.mouseUp(advanced);
  fireEvent.click(advanced);
  expect(onChange).toHaveBeenCalledWith("advanced");
});

test("clicking the open trigger closes the choices and restores its focus", () => {
  render(<ModeMenu mode="simple" disabled={false} onChange={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: "Reading mode: Simple" });
  fireEvent.click(trigger);
  fireEvent.mouseDown(trigger);
  fireEvent.mouseUp(trigger);
  fireEvent.click(trigger);
  expect(screen.queryByRole("group")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

test("opens beside the trigger and switches modes with the keyboard", () => {
  const onChange = vi.fn();
  render(<ModeMenu mode="simple" disabled={false} onChange={onChange} />);
  const trigger = screen.getByRole("button", { name: "Reading mode: Simple" });
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
  const simple = screen.getByRole("button", { name: "Simple", pressed: true });
  const advanced = screen.getByRole("button", { name: "Advanced", pressed: false });
  expect(document.activeElement).toBe(simple);
  expect(trigger.parentElement?.contains(advanced)).toBe(true);
  fireEvent.keyDown(simple, { key: "ArrowDown" });
  expect(document.activeElement).toBe(advanced);
  fireEvent.click(advanced);
  expect(onChange).toHaveBeenCalledWith("advanced");
  expect(screen.queryByRole("group")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

test("dismisses on Escape, outside click, and focus leaving without switching", () => {
  const onChange = vi.fn();
  render(<ModeMenu mode="advanced" disabled={false} onChange={onChange} />);
  const trigger = screen.getByRole("button", { name: "Reading mode: Advanced" });
  fireEvent.click(trigger);
  fireEvent.keyDown(screen.getByRole("button", { name: "Advanced" }), { key: "Escape" });
  expect(document.activeElement).toBe(trigger);
  expect(screen.queryByRole("group")).toBeNull();
  fireEvent.click(trigger);
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("group")).toBeNull();
  fireEvent.click(trigger);
  fireEvent.blur(screen.getByRole("button", { name: "Advanced" }), { relatedTarget: document.body });
  expect(screen.queryByRole("group")).toBeNull();
  expect(onChange).not.toHaveBeenCalled();
});

test("locks and dismisses the choices when Narration becomes active", () => {
  const onChange = vi.fn();
  const { rerender } = render(<ModeMenu mode="simple" disabled={false} onChange={onChange} />);
  fireEvent.click(screen.getByRole("button", { name: "Reading mode: Simple" }));
  rerender(<ModeMenu mode="simple" disabled onChange={onChange} />);
  expect(screen.queryByRole("group")).toBeNull();
  expect(screen.getByRole("button", { name: "Reading mode: Simple" })).toHaveProperty("disabled", true);
  rerender(<ModeMenu mode="simple" disabled={false} onChange={onChange} />);
  expect(screen.queryByRole("group")).toBeNull();
});
