import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Vitest runs with the workspace root (web/) as cwd. jsdom computes no layout and
// does not resolve `var()`, so this guards XERK-106 at the source: the live cue
// band must keep the same vertical margin above it (against the Stop/Pause row)
// as below it (against the transcript card).
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

/** The declarations of the first rule whose selector list matches `selector`. */
const ruleBody = (css: string, selector: string): string => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`(?:^|\\})[^{}]*${escaped}[^{}]*\\{([^}]*)\\}`));
  if (!match) throw new Error(`no rule found for ${selector}`);
  return match[1];
};

/** The [top, bottom] halves of a shorthand `margin:` declaration. */
const verticalMargin = (body: string): [string, string] => {
  const decl = body.match(/(?:^|;)\s*margin:\s*([^;]+)/);
  if (!decl) throw new Error(`no margin shorthand in: ${body.trim()}`);
  const parts = decl[1].trim().split(/\s+/);
  return [parts[0], parts[2] ?? parts[0]];
};

describe("live cue band spacing (XERK-106)", () => {
  const css = readText("src/styles.css");

  it("gives the cue band equal margins above and below", () => {
    const [top, bottom] = verticalMargin(ruleBody(css, ".cue-band"));
    expect(top).toBe(bottom);
  });

  it("matches the transcript card's own vertical margin", () => {
    // The gap below the band is the collapse of its bottom margin with the
    // card's top margin; equal values keep both sides of the band identical.
    const [bandTop] = verticalMargin(ruleBody(css, ".cue-band"));
    const [cardTop] = verticalMargin(ruleBody(css, "section, .detail, .card"));
    expect(bandTop).toBe(cardTop);
  });

  it("leaves no zero margin against the capture controls", () => {
    const [top] = verticalMargin(ruleBody(css, ".cue-band"));
    expect(top).not.toBe("0");
    expect(top).toMatch(/^var\(--space-\d\)$/);
  });
});
