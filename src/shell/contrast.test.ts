import { expect, test } from "vitest";
import STYLESHEET from "../App.css?raw";

const luminance = (hex: string) => {
  const channels = [1, 3, 5].map((at) => parseInt(hex.slice(at, at + 2), 16) / 255);
  const [r, g, b] = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

// WCAG's sRGB ratio, which is what "4.5:1" in SC 1.4.3 means.
const contrast = (a: string, b: string) => {
  const [lighter, darker] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (lighter + 0.05) / (darker + 0.05);
};

const colorOf = (selector: string) => {
  const rule = new RegExp(
    `(?:^|\\n)${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*(?:,[^{]*)?\\{([^}]*)\\}`,
  ).exec(STYLESHEET);
  expect(rule, `no rule for ${selector}`).not.toBeNull();
  const color = /(?:^|;|\s)color:\s*(#[0-9a-f]{6})/.exec(rule?.[1] ?? "");
  expect(color, `no color on ${selector}`).not.toBeNull();
  return color?.[1] as string;
};

const NORMAL_TEXT: [selector: string, surface: string][] = [
  [".firstrun__card--failed .firstrun__line", "#1d1d1d"],
  [".firstrun__detail", "#1d1d1d"],
  [".firstrun__aside", "#1d1d1d"],
  [".firstrun__retry", "#1d1d1d"],
  [".firstrun__licences", "#1d1d1d"],
  [".firstrun__licence", "#1d1d1d"],
  [".side__heading", "#101010"],
  [".side__foot", "#101010"],
  [".history__open", "#252525"],
  [".history__meta", "#252525"],
  [".history__marker--gaps", "#252525"],
  [".carousel__note", "#101010"],
  [".carousel__step", "#101010"],
  [".composer__estimate", "#1d1d1d"],
  [".composer__estimate--over", "#1d1d1d"],
  [".composer__notice", "#1d1d1d"],
  [".composer__source::placeholder", "#1d1d1d"],
  [".model-menu__name", "#303030"],
  [".model-menu__browse", "#1c1c1f"],
  [".model-menu__notice", "#1c1c1f"],
  [".tier", "#2a2a2e"],
  [".tier--instant", "#0f2a1c"],
  [".tier--expressive", "#231a3a"],
  [".narration__line", "#101010"],
  [".narration--failed .narration__line", "#101010"],
  [".narration--ready .narration__line", "#101010"],
  [".opened__gaps", "#101010"],
  [".opened__block--played", "#101010"],
  [".player__clock", "#101010"],
  [".player__voice-model", "#1b1b1b"],
  [".player__voice-name", "#1b1b1b"],
  [".player__speed", "#1b1b1b"],
  [".main__export", "#101010"],
  [".bar__button", "#101010"],
  [".player__faster", "#1d1d1d"],
  [".player__notice", "#101010"],
  [".player__problem", "#101010"],
  [".player__generation", "#101010"],
  [".sheet__lede", "#1d1d1d"],
  ['.sheet__stop[aria-disabled="true"]', "#b8695b"],
  ['.sheet__go[aria-disabled="true"]', "#c9c8c5"],
  [".sheet__close:disabled", "#1d1d1d"],
  [".sheet__empty", "#1d1d1d"],
  [".model__checking", "#1d1d1d"],
  [".model__detail", "#1d1d1d"],
  [".model__credit", "#1d1d1d"],
  [".model__licence", "#1d1d1d"],
  [".licence__facts", "#101010"],
  [".licence__facts dt", "#101010"],
  [".licence__source", "#101010"],
  [".licence__note", "#101010"],
  [".licence__text", "#1d1d1d"],
  [".model__progress--failed", "#1d1d1d"],
];

test.each(NORMAL_TEXT)("%s is readable on %s", (selector, surface) => {
  expect(contrast(colorOf(selector), surface)).toBeGreaterThanOrEqual(4.5);
});

// A focus ring is a non-text contrast (SC 1.4.11): 3:1 against every
// surface it can sit on, including the border it replaces.
test("the focus ring shows against every surface it sits on", () => {
  const ring = /--focus-ring:\s*(#[0-9a-f]{6})/.exec(STYLESHEET)?.[1];
  expect(ring).toBeDefined();
  for (const surface of ["#101010", "#1d1d1d", "#303030"]) {
    expect(contrast(ring as string, surface), surface).toBeGreaterThanOrEqual(3);
  }
});
