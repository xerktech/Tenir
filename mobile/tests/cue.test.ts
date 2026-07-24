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
    expect(history).toContain("<CueDisclosure key={item.cue.cueId} title={item.cue.title} body={item.cue.body} />");
    // The modal open/close state is gone from the detail screen.
    expect(history).not.toContain("CueModal");
    expect(history).not.toContain("InlineCue");
    expect(history).not.toContain("setOpenCue");
  });
});

describe("released cues are embedded inline in the live transcript (XERK-108)", () => {
  const live = readText("src/screens/Live.tsx");

  it("interleaves finalized turns and past cues via the shared liveTranscript helper", () => {
    // Built from the shared timeline helper, so the interleave matches web/even.
    expect(live).toContain("liveTranscript");
    expect(live).toContain("const items = liveTranscript(state.segments, state.pastCues)");
    // The transcript renders that merged list, not the bare segment array.
    expect(live).not.toContain("state.segments.map(");
    expect(live).toContain("items.map((item) =>");
  });

  it("renders a past cue with the same inline dropdown history uses", () => {
    expect(live).toContain(
      "<CueDisclosure key={`cue-${item.cue.id}`} title={item.cue.title} body={item.cue.body} />",
    );
    // Past cues count as content so a transcript of only-reviewed cues still shows.
    expect(live).toContain("state.pastCues.length > 0");
  });
});
