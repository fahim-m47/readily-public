import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import AdvancedControls from "./AdvancedControls";
import type { Control, ControlSettings, Overrides } from "../engine/advanced";

afterEach(cleanup);

const parameters: Record<string, Control> = {
  steps: { label: "Steps", kind: "integer", unit: "steps", description: "Generation steps", hardRange: { min: 1, max: 30, minExclusive: false }, recommendedRange: { min: 4, max: 12, minExclusive: false }, nullable: false },
  seed: { label: "Seed", kind: "integer", unit: "seed", description: "Generation seed", hardRange: { min: 0, max: null, minExclusive: false }, recommendedRange: null, nullable: true },
  prepare_first: { label: "Prepare first", kind: "boolean", unit: "on/off", description: "Prepare all audio before listening" },
  word_timing: { label: "Word timing", kind: "choice", unit: "mode", description: "How word times are found", choices: ["off", "whisperx:tiny"] },
};

const SETTINGS: ControlSettings = {
  overrides: {},
  effectiveValues: { steps: 8, seed: null, prepare_first: false, word_timing: "off" },
};

const mount = (settings: ControlSettings | null = SETTINGS, failure: string | null = null) => {
  const save = vi.fn<(overrides: Overrides) => Promise<void>>(async () => {});
  const panel = (shown: ControlSettings | null) => (
    <AdvancedControls
      name="Supertonic · M1"
      parameters={parameters}
      wordTimingModels={[{ id: "whisperx:tiny", name: "WhisperX tiny" }]}
      settings={shown}
      failure={failure}
      save={save}
    />
  );
  const { rerender } = render(panel(settings));
  return { save, show: (shown: ControlSettings) => rerender(panel(shown)) };
};

test("every declared Control gets a field named and bounded by the schema", () => {
  mount();
  expect(screen.getByRole("spinbutton", { name: "Steps" })).toBeTruthy();
  expect(screen.getByRole("checkbox", { name: "Prepare first" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "Word timing" })).toBeTruthy();
  // The band worth exploring is the only range spelled out; the hard range
  // lives on the field itself.
  expect(screen.getByText("Generation steps Try 4 to 12.")).toBeTruthy();
  expect(screen.queryByText(/Hard range/)).toBeNull();
  const steps = screen.getByRole("spinbutton", { name: "Steps" });
  expect(steps).toHaveProperty("min", "1");
  expect(steps).toHaveProperty("max", "30");
  expect(steps).toHaveProperty("step", "1");
  expect(steps).toHaveProperty("required", true);
  // Only `word_timing` needs a name the schema does not carry.
  expect(screen.getByRole("option", { name: "WhisperX tiny" })).toBeTruthy();
  expect(screen.getByText("Generation seed")).toBeTruthy();
});

test("a nullable Control with no value offers Automatic rather than a number", () => {
  mount();
  const seed = screen.getByRole("spinbutton", { name: "Seed" });
  expect(seed).toHaveProperty("value", "");
  expect(seed).toHaveProperty("placeholder", "Automatic");
});

test("saving pins only what differs from the effective value", async () => {
  const { save } = mount();
  expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByRole("spinbutton", { name: "Steps" }), { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "Save 1 change" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith({ steps: 20 }));
  expect(await screen.findByText("Saved for new Narrations with this Voice.")).toBeTruthy();
});

test("every kind of field is saved from what the reader left in it", async () => {
  const { save } = mount();
  fireEvent.click(screen.getByRole("checkbox", { name: "Prepare first" }));
  fireEvent.change(screen.getByRole("combobox", { name: "Word timing" }), {
    target: { value: "whisperx:tiny" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save 2 changes" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith({ prepare_first: true, word_timing: "whisperx:tiny" }),
  );
});

test("resetting everything clears every Override, whatever is in the fields", async () => {
  const { save } = mount({ overrides: { steps: 20 }, effectiveValues: { ...SETTINGS.effectiveValues, steps: 20 } });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Steps" }), { target: { value: "3" } });
  fireEvent.click(screen.getByRole("button", { name: "Reset all" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith({}));
});

test("only a pinned control offers a way back, and it drops that one Override", async () => {
  const { save } = mount({ overrides: { steps: 20, seed: 7 }, effectiveValues: { ...SETTINGS.effectiveValues, steps: 20, seed: 7 } });
  expect(screen.getAllByRole("button", { name: /^Reset .* to default$/ })).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Reset Steps to default" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith({ seed: 7 }));
  expect(await screen.findByText("Reset to the Catalog default.")).toBeTruthy();
});

test("nothing on its default offers Reset all", () => {
  mount();
  expect(screen.queryByRole("button", { name: "Reset all" })).toBeNull();
  expect(screen.queryByRole("button", { name: /^Reset .* to default$/ })).toBeNull();
});

test("an Override the reader did not touch is carried through the save", async () => {
  const { save } = mount({ overrides: { seed: 7 }, effectiveValues: { ...SETTINGS.effectiveValues, seed: 7 } });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Steps" }), { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "Save 1 change" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith({ seed: 7, steps: 20 }));
});

test("a refused save is reported and the typed value stays on screen", async () => {
  const save = vi.fn<(overrides: Overrides) => Promise<void>>(async () => {
    throw new Error("Steps must be a whole number.");
  });
  render(
    <AdvancedControls name="Supertonic · M1" parameters={parameters} wordTimingModels={[]}
      settings={SETTINGS} failure={null} save={save} />,
  );
  const steps = screen.getByRole("spinbutton", { name: "Steps" });
  fireEvent.change(steps, { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "Save 1 change" }));
  expect(await screen.findByText("Steps must be a whole number.")).toBeTruthy();
  expect(steps).toHaveProperty("value", "20");
});

test("controls that could not be read say so instead of showing an empty form", () => {
  mount(null, "Controls could not be read. Select a different Voice and come back to try again.");
  expect(screen.queryByRole("spinbutton")).toBeNull();
  expect(
    screen.getByText("Controls could not be read. Select a different Voice and come back to try again."),
  ).toBeTruthy();
});

test("the Engine's answer to a save is what the fields show afterwards", async () => {
  const { save, show } = mount();
  const steps = screen.getByRole("spinbutton", { name: "Steps" });
  fireEvent.change(steps, { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "Save 1 change" }));
  await waitFor(() => expect(save).toHaveBeenCalled());

  // The Engine clamps to its own hard range, and what it stored is what the
  // next Narration will read with.
  show({ overrides: { steps: 12 }, effectiveValues: { ...SETTINGS.effectiveValues, steps: 12 } });
  expect(screen.getByRole("spinbutton", { name: "Steps" })).toHaveProperty("value", "12");
  expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
});

test("a field mid-edit keeps its text when another control is reset", async () => {
  const { save, show } = mount({ overrides: { seed: 7 }, effectiveValues: { ...SETTINGS.effectiveValues, seed: 7 } });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Steps" }), { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "Reset Seed to default" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith({}));
  show({ overrides: {}, effectiveValues: { ...SETTINGS.effectiveValues, seed: null } });
  expect(screen.getByRole("spinbutton", { name: "Seed" })).toHaveProperty("value", "");
  expect(screen.getByRole("spinbutton", { name: "Steps" })).toHaveProperty("value", "20");
});

test("a field mid-edit keeps its text when Settings saves through the same hook", () => {
  const { show } = mount(SETTINGS);
  fireEvent.change(screen.getByRole("spinbutton", { name: "Steps" }), { target: { value: "20" } });
  show({ overrides: { seed: 7 }, effectiveValues: { ...SETTINGS.effectiveValues, seed: 7 } });
  expect(screen.getByRole("spinbutton", { name: "Seed" })).toHaveProperty("value", "7");
  expect(screen.getByRole("spinbutton", { name: "Steps" })).toHaveProperty("value", "20");
});
