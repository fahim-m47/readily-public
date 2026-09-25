import { useState } from "react";
import type { Control, ControlRange, ControlSettings, ControlValue, Overrides } from "../engine/advanced";

type Props = {
  // The chosen Voice, as the shell describes it.
  name: string;
  parameters: Record<string, Control>;
  wordTimingModels: { id: string; name: string }[];
  settings: ControlSettings | null;
  failure: string | null;
  save: (overrides: Overrides) => Promise<void>;
};

type Draft = Record<string, string>;

// The panel's three groups. A key named here belongs to its group; every
// other key is a Voice Model's own sampling or decoding control and lands
// in Generation, ahead of the seed.
const GROUPS = [
  { title: "Generation", keys: [] as string[] },
  {
    title: "Blocks and pauses",
    keys: ["chunk_budget_chars", "first_block_chars", "pause_sentence_ms", "pause_paragraph_break_ms"],
  },
  { title: "Playback", keys: ["word_timing", "prepare_first"] },
];

const grouped = (parameters: Record<string, Control>) => {
  const placed = new Set(GROUPS.flatMap((group) => group.keys));
  const own = Object.keys(parameters).filter((key) => key !== "seed" && !placed.has(key));
  return GROUPS.map((group) => ({
    title: group.title,
    keys:
      group.title === "Generation"
        ? [...own, ...("seed" in parameters ? ["seed"] : [])]
        : group.keys.filter((key) => key in parameters),
  })).filter((group) => group.keys.length > 0);
};

const bandText = (range: ControlRange) =>
  range.max === null ? `Try ${range.min} or more.` : `Try ${range.min} to ${range.max}.`;

const format = (value: ControlValue | undefined) =>
  value === null || value === undefined ? "" : String(value);
const parse = (control: Control, text: string): ControlValue =>
  control.kind === "boolean"
    ? text === "true"
    : control.kind === "choice"
      ? text
      : text === ""
        ? null
        : Number(text);

const draftOf = (parameters: Record<string, Control>, values: Overrides): Draft =>
  Object.fromEntries(Object.keys(parameters).map((key) => [key, format(values[key])]));

const choiceLabel = (
  name: string,
  choice: string,
  wordTimingModels: Props["wordTimingModels"],
) => {
  if (name === "word_timing") {
    return choice === "off"
      ? "Off"
      : (wordTimingModels.find((model) => model.id === choice)?.name ?? choice);
  }
  return choice.charAt(0).toUpperCase() + choice.slice(1);
};

// Mounted per Voice: what was just saved, and any complaint about saving it,
// are facts about one Voice and must not follow the reader to the next.
export default function AdvancedControls({
  name,
  parameters,
  wordTimingModels,
  settings,
  failure,
  save,
}: Props) {
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  // The fields hold the Engine's last word plus whatever has been typed since.
  // Adjusted during render rather than in an effect: an effect would show a
  // saved value beside the old one for a frame. The Engine's answer replaces
  // the fields a save submitted, since it may have clamped them; a field
  // mid-edit keeps its text across a save that did not include it, such as
  // one control's reset.
  const [shown, setShown] = useState<ControlSettings | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [submitted, setSubmitted] = useState<"all" | string[]>([]);
  if (shown !== settings) {
    setShown(settings);
    if (settings === null) {
      setDraft({});
    } else {
      const fresh = draftOf(parameters, settings.effectiveValues);
      setDraft(
        shown === null || submitted === "all"
          ? fresh
          : Object.fromEntries(
              Object.keys(parameters).map((key) => [
                key,
                submitted.includes(key) || draft[key] === format(shown.effectiveValues[key])
                  ? fresh[key]
                  : draft[key],
              ]),
            ),
      );
    }
  }

  const commit = async (overrides: Overrides, keys: "all" | string[], saved: string) => {
    setSubmitted(keys);
    setSaving(true);
    setNotice("");
    try {
      await save(overrides);
      setNotice(saved);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Controls could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  const edited = (current: ControlSettings) =>
    Object.entries(parameters).filter(
      ([key, control]) => parse(control, draft[key] ?? "") !== current.effectiveValues[key],
    );

  const changed = (current: ControlSettings) => {
    const changes: Overrides = { ...current.overrides };
    for (const [key, control] of edited(current)) changes[key] = parse(control, draft[key] ?? "");
    return changes;
  };

  const reset = (current: ControlSettings, key: string) => {
    const kept = Object.fromEntries(Object.entries(current.overrides).filter(([name]) => name !== key));
    void commit(kept, [key], "Reset to the Catalog default.");
  };

  const field = (key: string, control: Control) => {
    const id = `control-${key}`;
    const text = draft[key] ?? "";
    const pinned = settings !== null && key in settings.overrides;
    const set = (next: string) => setDraft((current) => ({ ...current, [key]: next }));
    const note = [
      control.description,
      control.kind !== "boolean" && control.kind !== "choice" && control.recommendedRange
        ? bandText(control.recommendedRange)
        : "",
    ]
      .filter(Boolean)
      .join(" ");
    return (
      <div className={`control${pinned ? " control--pinned" : ""}`} key={key}>
        <label className="control__label" htmlFor={id}>
          {control.label}
        </label>
        {control.kind === "boolean" ? (
          <input
            className="control__check"
            id={id}
            aria-describedby={`${id}-note`}
            type="checkbox"
            checked={text === "true"}
            onChange={(event) => set(String(event.target.checked))}
          />
        ) : control.kind === "choice" ? (
          <select
            className="control__field"
            id={id}
            aria-describedby={`${id}-note`}
            value={text} onChange={(event) => set(event.target.value)}>
            {control.choices.map((choice) => (
              <option key={choice} value={choice}>
                {choiceLabel(key, choice, wordTimingModels)}
              </option>
            ))}
          </select>
        ) : (
          <span className="control__number">
            <input
              className="control__field"
              id={id}
              aria-describedby={`${id}-note`}
              type="number"
              value={text}
              onChange={(event) => set(event.target.value)}
              min={control.hardRange.min}
              max={control.hardRange.max ?? undefined}
              step={control.kind === "integer" ? 1 : "any"}
              required={!control.nullable}
              placeholder={control.nullable ? "Automatic" : undefined}
            />
            {control.unit && <span className="control__unit">{control.unit}</span>}
          </span>
        )}
        <p className="control__note" id={`${id}-note`}>
          {note}
          {pinned && settings && (
            <>
              {" "}
              <button
                className="control__reset"
                type="button"
                aria-label={`Reset ${control.label} to default`}
                disabled={saving}
                onClick={() => reset(settings, key)}
              >
                Reset to default
              </button>
            </>
          )}
        </p>
      </div>
    );
  };

  const pending = settings === null ? [] : edited(settings);
  const pinnedCount = settings === null ? 0 : Object.keys(settings.overrides).length;

  return (
    <section className="controls" aria-label="Controls">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (settings) void commit(changed(settings), "all", "Saved for new Narrations with this Voice.");
        }}
      >
        <div className="controls__top">
          <div>
            <h2 className="controls__title">Controls</h2>
            <p className="controls__lede">
              For {name}. New Narrations use these; saved ones keep their own.
            </p>
          </div>
          <div className="controls__actions">
            {pinnedCount > 0 && (
              <button
                className="controls__secondary"
                type="button"
                disabled={saving}
                onClick={() => {
                  void commit({}, "all", "Every control is back on its Catalog default.");
                }}
              >
                Reset all
              </button>
            )}
            <button className="controls__save" type="submit" disabled={saving || pending.length === 0}>
              {pending.length === 0
                ? "Save"
                : `Save ${pending.length} ${pending.length === 1 ? "change" : "changes"}`}
            </button>
          </div>
        </div>
        <p className="controls__notice" role="status">
          {notice || failure || ""}
        </p>
        {settings && (
          <fieldset disabled={saving} className="controls__groups">
            {grouped(parameters).map((group) => (
              <div className="controls__group" key={group.title}>
                <h3 className="controls__heading">{group.title}</h3>
                {group.keys.map((key) => field(key, parameters[key]))}
              </div>
            ))}
          </fieldset>
        )}
      </form>
    </section>
  );
}
