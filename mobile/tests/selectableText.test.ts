import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level checks (the RN screens aren't rendered in this jsdom suite,
// mirroring tabBar.test.ts / updateBanner.test.ts). They guard XERK-104:
// conversation and history transcript text must be user-selectable so it can be
// long-pressed and copied — RN <Text> is not selectable by default, so the
// `selectable` prop has to be set explicitly. `selectable` on a <Text> also
// propagates to its nested <Text> children (so the history timing prefix rides
// along with the segment). Kept in parity with the web and even clients, which
// opt the same surfaces in via CSS `user-select: text`.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("selectable transcript text (XERK-104)", () => {
  it("Live: committed segments and the partial caption are selectable", () => {
    const src = readText("src/screens/Live.tsx");
    // The committed-segment <Text> wrapping {seg.text} carries `selectable`.
    expect(src).toMatch(/<Text selectable style=\{\{ color: colors\.text \}\}>\s*\{seg\.text\}/);
    // The live partial renders through <Muted selectable>.
    expect(src).toMatch(/<Muted selectable>\{`› \$\{state\.partial\}`\}<\/Muted>/);
  });

  it("History: the conversation-detail segment text is selectable", () => {
    const src = readText("src/screens/History.tsx");
    // The outer row <Text> is selectable; its nested timing <Text> inherits it.
    expect(src).toMatch(/<Text key=\{item\.seg\.segmentId\} selectable /);
  });

  it("Cues: the live band body and the inline disclosure body are selectable", () => {
    const src = readText("src/ui/cue.tsx");
    // The band paints `cue` — the active cue, or the one still fading out (XERK-107).
    expect(src).toMatch(/<Text selectable style=\{styles\.cardBody\}>\s*\{cue\.body\}/);
    // The history cue detail is now an inline dropdown (XERK-105), not a modal;
    // its expanded body stays selectable so it can still be copied.
    expect(src).toMatch(/<Text selectable style=\{styles\.disclosureBody\}>\s*\{body\}/);
  });

  it("Muted forwards a `selectable` prop to its underlying <Text>", () => {
    const src = readText("src/ui/components.tsx");
    expect(src).toMatch(/selectable\?: boolean/);
    expect(src).toMatch(/<Text style=\{styles\.muted\} selectable=\{selectable\}>/);
  });
});
