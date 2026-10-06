// @vitest-environment node
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

// Vitest runs from web/.
const css = readFileSync(resolve(process.cwd(), "src/styles/tokens.css"), "utf8");
const app = readFileSync(resolve(process.cwd(), "src/styles/app.css"), "utf8");

/** The custom properties declared in the first block that starts with `selector`. */
function block(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no block for ${selector}`);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
  const found: Record<string, string> = {};
  for (const match of body.matchAll(/(--color-[\w-]+):\s*([^;]+);/g)) found[match[1] as string] = (match[2] as string).trim();
  return found;
}

const light = block(":root");
const darkByQuery = block(':root:not([data-theme="light"])');
const darkByAttribute = block(':root[data-theme="dark"]');

function luminance(hex: string): number {
  const value = /^#([0-9a-f]{6})$/i.exec(hex)?.[1];
  if (value === undefined) throw new Error(`not a #rrggbb colour: ${hex}`);
  const channels = [0, 2, 4].map((offset) => parseInt(value.slice(offset, offset + 2), 16) / 255);
  const [r = 0, g = 0, b = 0] = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(foreground: string, background: string): number {
  const [a, b] = [luminance(foreground), luminance(background)].sort((x, y) => y - x) as [number, number];
  return (a + 0.05) / (b + 0.05);
}

const themes = { light, dark: darkByAttribute } as const;

// [foreground, background, minimum ratio]: 4.5 for text, 3 for the border of a control and the focus ring.
const PAIRS: [string, string, number][] = [
  ["--color-text", "--color-bg", 4.5],
  ["--color-text", "--color-surface", 4.5],
  ["--color-text", "--color-surface-sunken", 4.5],
  ["--color-text-muted", "--color-bg", 4.5],
  ["--color-text-muted", "--color-surface", 4.5],
  ["--color-text-muted", "--color-surface-sunken", 4.5],
  ["--color-link", "--color-bg", 4.5],
  ["--color-link", "--color-surface", 4.5],
  ["--color-on-accent", "--color-accent", 4.5],
  ["--color-on-accent", "--color-accent-hover", 4.5],
  ["--color-danger-text", "--color-danger-bg", 4.5],
  ["--color-warning-text", "--color-warning-bg", 4.5],
  ["--color-success-text", "--color-success-bg", 4.5],
  ["--color-info-text", "--color-info-bg", 4.5],
  ["--color-border-strong", "--color-surface", 3],
  ["--color-border-strong", "--color-bg", 3],
  ["--color-focus", "--color-bg", 3],
  ["--color-focus", "--color-surface", 3],
  ["--color-accent", "--color-surface", 3],
];

describe("design tokens", () => {
  for (const [name, colours] of Object.entries(themes)) {
    describe(`${name} theme`, () => {
      it.each(PAIRS)("%s on %s has a contrast of at least %s:1", (foreground, background, minimum) => {
        const ratio = contrast(colours[foreground] as string, colours[background] as string);
        expect(ratio, `${foreground} on ${background}`).toBeGreaterThanOrEqual(minimum);
      });
    });
  }

  it("keeps the dark set under the media query identical to the one under the attribute", () => {
    expect(darkByQuery).toEqual(darkByAttribute);
  });

  it("defines every colour of the light set in the dark set, and the reverse", () => {
    expect(Object.keys(darkByAttribute).sort()).toEqual(Object.keys(light).sort());
  });

  it("follows the system by default and lets the attribute choose either theme", () => {
    expect(css).toContain("@media (prefers-color-scheme: dark)");
    expect(css).toContain(':root[data-theme="light"]');
    expect(css).toContain(':root[data-theme="dark"]');
  });

  it("uses no colour in the stylesheet that is not a token", () => {
    expect(app).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(app).not.toMatch(/\brgba?\(/);
  });

  it("switches off animation for people who ask for reduced motion", () => {
    expect(app).toContain("@media (prefers-reduced-motion: reduce)");
  });

  it("has no inline-style escape hatches: the server's policy allows its own stylesheets only", () => {
    expect(app).not.toContain("@import");
  });
});
