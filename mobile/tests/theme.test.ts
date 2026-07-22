/**
 * Design-parity guard: the mobile Lumen palettes must carry the exact hex
 * values of the web SPA's CSS custom properties (CLAUDE.md: web and Android
 * ship the same design with Tenir's own colours). Parses web/src/styles.css so
 * a token edited on one platform without the other fails here.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { palettes, withAlpha, type Palette } from "../src/ui/theme";

// Vitest runs with the workspace root (mobile/) as cwd; the web SPA is a sibling.
const css = readFileSync(resolve(process.cwd(), "../web/src/styles.css"), "utf8");

/** The web CSS variable behind each mobile palette key. */
const TOKEN_FOR: Record<keyof Palette, string> = {
  bg: "--bg",
  surface: "--surface",
  surfaceRaised: "--surface-raised",
  border: "--border",
  borderStrong: "--border-strong",
  text: "--text",
  muted: "--text-muted",
  accent: "--accent",
  accentStrong: "--accent-strong",
  onAccent: "--accent-ink",
  danger: "--danger",
  onDanger: "--danger-ink",
  success: "--success",
  warning: "--warning",
};

/** Pull `--name: #hex;` out of one top-level block of the stylesheet. */
function cssToken(blockSelector: string, name: string): string {
  const block = css.split(blockSelector)[1]?.split("}")[0] ?? "";
  const m = block.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!m) throw new Error(`token ${name} not found under ${blockSelector}`);
  return m[1].toUpperCase();
}

describe("Lumen palette parity with the web SPA", () => {
  it("dark palette matches the :root tokens", () => {
    for (const [key, token] of Object.entries(TOKEN_FOR) as [keyof Palette, string][]) {
      expect(palettes.dark[key].toUpperCase(), `dark ${key}`).toBe(cssToken(":root {", token));
    }
  });

  it("light palette matches the [data-theme=\"light\"] tokens", () => {
    for (const [key, token] of Object.entries(TOKEN_FOR) as [keyof Palette, string][]) {
      expect(palettes.light[key].toUpperCase(), `light ${key}`).toBe(
        cssToken('[data-theme="light"] {', token),
      );
    }
  });

  it("keeps the signature teal accent (not the retired green)", () => {
    expect(palettes.dark.accent).toBe("#3FD9C9");
    expect(palettes.dark.accent).not.toBe("#3fb950");
  });
});

describe("withAlpha", () => {
  it("renders a hex colour as rgba at the given opacity", () => {
    expect(withAlpha("#3FD9C9", 0.45)).toBe("rgba(63, 217, 201, 0.45)");
    expect(withAlpha("#0E1116", 1)).toBe("rgba(14, 17, 22, 1)");
  });
});
