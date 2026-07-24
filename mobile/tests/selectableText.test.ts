import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level checks (the RN screens aren't rendered in this jsdom suite,
// mirroring tabBar.test.ts / updateBanner.test.ts). They guard XERK-104:
// conversation and history transcript text must be user-selectable AND a
// selection must be draggable across turns so it can be copied. RN only extends
// a selection within a single <Text> tree, so every turn has to render inside
// one selectable <Text> (newline-separated) rather than one <Text> per turn —
// otherwise each turn is its own selection island. Kept in parity with the web
// and even clients, whose DOM selection already spans the whole transcript.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("selectable transcript text (XERK-104)", () => {
  it("Live: all turns and the partial share ONE selectable <Text>", () => {
    const src = readText("src/screens/Live.tsx");
    // A single selectable block, not a <Text> (or ListItem) per segment.
    expect(src).toMatch(/<Text selectable style=\{\{ color: colors\.text, lineHeight: 22 \}\}>/);
    // Segments are mapped inside it and newline-separated (so the drag-select
    // spans them).
    expect(src).toMatch(/state\.segments\.map\(\(seg, i\) =>/);
    expect(src).toMatch(/\{i > 0 \? "\\n" : null\}/);
    // No per-turn ListItem wrapper remains around a transcript <Text>.
    expect(src).not.toMatch(/<ListItem key=\{seg\.id\}>/);
  });

  it("History: consecutive turns are grouped into one selectable <Text> run", () => {
    const src = readText("src/screens/History.tsx");
    // Consecutive segments are merged into a run...
    expect(src).toMatch(/function runs\(items: TranscriptItem\[\]\): Run\[\]/);
    expect(src).toMatch(/runs\(items\)\.map\(\(run, i\) =>/);
    // ...and each run renders as a single selectable <Text> whose segments are
    // newline-separated (a selection spans every turn in the run).
    expect(src).toMatch(/<Text key=\{`run-\$\{i\}`\} selectable /);
    expect(src).toMatch(/\{j > 0 \? "\\n" : null\}/);
  });

  it("Cues: the live band body and the inline disclosure body are selectable", () => {
    const src = readText("src/ui/cue.tsx");
    expect(src).toMatch(/<Text selectable style=\{styles\.cardBody\}>\s*\{activeCue\.body\}/);
    // The history cue detail is an inline dropdown (XERK-105); its expanded body
    // stays selectable so it can still be copied.
    expect(src).toMatch(/<Text selectable style=\{styles\.disclosureBody\}>\s*\{body\}/);
  });
});
