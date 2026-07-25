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
  it("Live: consecutive turns share ONE selectable <Text> run, cues break runs", () => {
    const src = readText("src/screens/Live.tsx");
    // Each run of consecutive turns is a single selectable <Text> (XERK-104),
    // with a released cue dropdown ending the run (XERK-108) — mirroring History.
    expect(src).toMatch(/<Text key=\{`run-\$\{i\}`\} selectable style=\{\{ color: colors\.text, lineHeight: 22 \}\}>/);
    // Turns are mapped inside the run and newline-separated (so the drag-select
    // spans them).
    expect(src).toMatch(/run\.segs\.map\(\(seg, j\) =>/);
    expect(src).toMatch(/\{j > 0 \? "\\n" : null\}/);
    // No per-turn ListItem wrapper remains around a transcript <Text>.
    expect(src).not.toMatch(/<ListItem key=/);
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

  it("History: a per-open cue toggle can hide cues so the transcript is one run", () => {
    const src = readText("src/screens/History.tsx");
    // Cues default to shown on every open (state resets when Detail remounts).
    expect(src).toMatch(/const \[showCues, setShowCues\] = useState\(true\);/);
    // Hiding cues filters them out of the timeline, so runs() yields one run.
    expect(src).toMatch(/timeline\(conv\)\.filter\(\(it\) => showCues \|\| it\.kind === "segment"\)/);
    // The toggle is only offered when there are cues to hide.
    expect(src).toMatch(/const hasCues = \(conv\.cues\?\.length \?\? 0\) > 0;/);
    expect(src).toMatch(/hasCues && \(/);
    expect(src).toMatch(/<Switch/);
  });

  it("Cues: the live band body and the inline disclosure body are selectable", () => {
    const src = readText("src/ui/cue.tsx");
    // The band paints `cue` — the active cue, or the one still fading out (XERK-107).
    expect(src).toMatch(/<Text selectable style=\{styles\.cardBody\}>\s*\{cue\.body\}/);
    // The history cue detail is an inline dropdown (XERK-105); its expanded body
    // stays selectable so it can still be copied.
    expect(src).toMatch(/<Text selectable style=\{styles\.disclosureText\}>\s*\{body\}/);
  });
});
