export type ControlValue = number | string | boolean | null;
export type Overrides = Record<string, ControlValue>;
export type ControlRange = { min: number; max: number | null; minExclusive: boolean };
type Declaration = { label: string; unit: string; description: string };
export type Control = Declaration & (
  | { kind: "integer" | "number"; hardRange: ControlRange; recommendedRange: ControlRange | null; nullable: boolean }
  | { kind: "choice"; choices: string[] }
  | { kind: "boolean" }
);
export type ControlSettings = { overrides: Overrides; effectiveValues: Overrides };
export type TakeAction = "reroll" | "A" | "B";
export type BlockDiagnostics = {
  ordinal: number;
  recordHash: string;
  seed: number;
  cacheHit: boolean;
  wordTiming: "spoken" | "matched" | "estimated";
  supportModel: string | null;
  take: "A" | "B";
  hasComparison: boolean;
};
export type Diagnostics = {
  audioSecondsPerSecond: number | null;
  readySecondsAhead: number;
  preparingBlock: number | null;
  generationComplete: boolean;
  playingBlock: BlockDiagnostics | null;
  retries: number;
  cutoffs: number;
  ringStarvations: number;
  deviceUnderflows: number;
};

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const isCount = (value: unknown): value is number => isNumber(value) && Number.isInteger(value) && value >= 0;
const isRange = (value: unknown): value is ControlRange => isObject(value) &&
  isNumber(value.min) && (value.max === null || isNumber(value.max)) && typeof value.minExclusive === "boolean";
const isControl = (value: unknown): value is Control => {
  if (!isObject(value)) return false;
  if (typeof value.label !== "string" || typeof value.unit !== "string" || typeof value.description !== "string") return false;
  switch (value.kind) {
    case "boolean": return true;
    case "choice": return Array.isArray(value.choices) && value.choices.every((choice: unknown) => typeof choice === "string");
    case "number":
    case "integer": return isRange(value.hardRange) && (value.recommendedRange === null || isRange(value.recommendedRange)) && typeof value.nullable === "boolean";
    default: return false;
  }
};
export const isControlSchema = (value: unknown): value is Record<string, Control> =>
  isObject(value) && Object.values(value).every(isControl);
const isValues = (value: unknown): value is Overrides => isObject(value) && Object.values(value).every(
  (item) => item === null || typeof item === "boolean" || typeof item === "string" || isNumber(item),
);
export const controlSettings = (value: unknown): ControlSettings => {
  if (!isObject(value) || !isValues(value.overrides) || !isValues(value.effectiveValues)) {
    throw new Error("The Engine sent controls this app cannot read.");
  }
  return { overrides: value.overrides, effectiveValues: value.effectiveValues };
};
const isBlock = (value: unknown): value is BlockDiagnostics => isObject(value) &&
  isCount(value.ordinal) && typeof value.recordHash === "string" && /^[0-9a-f]{64}$/.test(value.recordHash) &&
  isCount(value.seed) && typeof value.cacheHit === "boolean" &&
  (value.wordTiming === "spoken" || value.wordTiming === "matched" || value.wordTiming === "estimated") &&
  (value.supportModel === null || typeof value.supportModel === "string") &&
  (value.take === "A" || value.take === "B") && typeof value.hasComparison === "boolean";
export const isDiagnostics = (value: unknown): value is Diagnostics => isObject(value) &&
  (value.audioSecondsPerSecond === null || isNumber(value.audioSecondsPerSecond)) &&
  isNumber(value.readySecondsAhead) && (value.preparingBlock === null || isCount(value.preparingBlock)) &&
  typeof value.generationComplete === "boolean" &&
  (value.playingBlock === null || isBlock(value.playingBlock)) &&
  [value.retries, value.cutoffs, value.ringStarvations, value.deviceUnderflows].every(isCount);
