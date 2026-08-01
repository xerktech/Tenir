import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level checks (the RN screens aren't rendered in this jsdom suite,
// mirroring selectableText.test.ts). They guard XERK-160: a non-English turn's
// English translation renders turn-by-turn under the original on Live and
// History — and INSIDE the selectable run <Text>, so drag-selection still
// crosses it (XERK-104) instead of the run splitting at every translated turn.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("live translations (XERK-160)", () => {
  it("Live: a translated turn renders its translation inside the run", () => {
    const src = readText("src/screens/Live.tsx");
    expect(src).toMatch(/seg\.translation \? <TranslationText text=\{seg\.translation\} \/> : null/);
  });

  it("History: a stored translation renders inside the run", () => {
    const src = readText("src/screens/History.tsx");
    expect(src).toMatch(/seg\.translation \? <TranslationText text=\{seg\.translation\} \/> : null/);
  });

  it("Live and History: a translated turn's original text leads with its source-language chip", () => {
    // The counterpart of the translation's "EN" tag: the original is tagged
    // with the language it was spoken in — only when a translation landed AND
    // the language was actually detected (run-inherited turns stay untagged).
    for (const screen of ["src/screens/Live.tsx", "src/screens/History.tsx"]) {
      const src = readText(screen);
      expect(src).toMatch(
        /seg\.translation && seg\.lang \? <LangTagText lang=\{seg\.lang\} \/> : null/,
      );
    }
  });

  it("TranslationText is a nested <Text> fragment, not a View, so runs stay selectable", () => {
    const src = readText("src/ui/components.tsx");
    const fn = src.slice(src.indexOf("export function TranslationText"));
    const body = fn.slice(0, fn.indexOf("\n}"));
    // Leads with a newline onto its own line, tagged "EN", inside Text elements only.
    expect(body).toMatch(/\{"\\n"\}/);
    expect(body).toMatch(/<LangTagText lang="en" \/>/);
    expect(body).not.toMatch(/<View/);
  });

  it("LangTagText is a nested <Text> fragment rendering the uppercased code", () => {
    const src = readText("src/ui/components.tsx");
    const fn = src.slice(src.indexOf("export function LangTagText"));
    const body = fn.slice(0, fn.indexOf("\n}"));
    expect(body).toMatch(/lang\.toUpperCase\(\)/);
    expect(body).not.toMatch(/<View/);
  });
});
