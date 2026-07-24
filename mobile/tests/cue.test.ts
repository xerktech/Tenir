import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level checks (the RN components aren't rendered in this jsdom suite,
// mirroring tabBar.test.ts / appIcon.test.ts). They guard the XERK-105 parity
// change: the history cue is an inline dropdown, collapsed by default, not a
// click-through modal popup.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("history cue is an inline dropdown, not a popup (XERK-105)", () => {
  const cue = readText("src/ui/cue.tsx");
  const history = readText("src/screens/History.tsx");

  it("exposes a CueDisclosure and drops the modal-based surfaces", () => {
    expect(cue).toContain("export function CueDisclosure(");
    // The old popup surface and its inline opener are gone.
    expect(cue).not.toContain("export function CueModal(");
    expect(cue).not.toContain("export function InlineCue(");
    // No React Native Modal is imported any more — the detail is inline.
    expect(cue).not.toMatch(/import\s*\{[^}]*\bModal\b[^}]*\}\s*from\s*"react-native"/);
  });

  it("defaults the dropdown to minimized and toggles on press", () => {
    // Collapsed by default: local open state starts false.
    expect(cue).toContain("useState(false)");
    expect(cue).toContain("setOpen((o) => !o)");
    // The body only renders once expanded, in place (no modal/backdrop), and
    // stays selectable so it can be copied (XERK-104).
    expect(cue).toContain("{open && (");
    expect(cue).toContain("<Text selectable style={styles.disclosureBody}>");
    expect(cue).not.toContain("styles.backdrop");
    // Expanded state is exposed to assistive tech.
    expect(cue).toContain("accessibilityState={{ expanded: open }}");
  });

  it("wires the history transcript to the inline dropdown", () => {
    expect(history).toContain("CueDisclosure");
    // A cue ends a run of selectable turns (XERK-104), so it renders from the
    // run's `cue` rather than a raw timeline item — still the inline dropdown.
    expect(history).toContain("<CueDisclosure key={run.cue.cueId} title={run.cue.title} body={run.cue.body} />");
    // The modal open/close state is gone from the detail screen.
    expect(history).not.toContain("CueModal");
    expect(history).not.toContain("InlineCue");
    expect(history).not.toContain("setOpenCue");
  });
});

describe("live cue floats over the transcript (XERK-107)", () => {
  const cue = readText("src/ui/cue.tsx");
  const live = readText("src/screens/Live.tsx");

  it("takes the band out of the column and lays it over the transcript", () => {
    // Absolutely placed against the top of its stage, above the captions.
    expect(cue).toContain('band: { position: "absolute", top: 0, left: 0, right: 0, zIndex: 2');
    // Click-through band so the transcript underneath still scrolls; the card
    // keeps its own touches so the cue text stays long-pressable (XERK-104).
    expect(cue).toContain('pointerEvents="box-none"');
    // Opaque fill — the captions it covers must not read through it.
    expect(cue).toContain("backgroundColor: mix(colors.accent, colors.surfaceRaised, 0.14)");
    // …not the translucent wash it used to sit on when it was in the flow.
    expect(cue).not.toMatch(/card: \{[^}]*backgroundColor: withAlpha\(/);
  });

  it("nests the band with the transcript in a positioning stage", () => {
    // The stage fills the column below the controls; the band overlays it, so
    // an arriving cue can't shove the captions down (and back up on release).
    expect(live).toContain("stage: { flex: 1 }");
    expect(live).toMatch(/<View style=\{styles\.stage\}>[\s\S]*<LiveCueBand[\s\S]*<\/View>/);
    // The band renders after the ScrollView, not before it in the column.
    expect(live.indexOf("<ScrollView")).toBeLessThan(live.indexOf("<LiveCueBand"));
  });

  it("fades a released cue out instead of blinking it away", () => {
    // Shared timing with the web SPA so both surfaces feel identical.
    expect(cue).toContain("CUE_EXIT_MS");
    expect(cue).toContain("duration: CUE_EXIT_MS");
    // The released cue stays painted for the fade, then unmounts.
    expect(cue).toContain("setTimeout(() => setPainted(null), CUE_EXIT_MS)");
    expect(cue).toContain("exiting: !activeCue && painted != null");
    expect(cue).toContain("<Animated.View");
  });
});
