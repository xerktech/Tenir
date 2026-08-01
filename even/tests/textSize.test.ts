import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// jsdom does not compute stylesheet rules, so — like selectableText.test.ts —
// this guards XERK-195 at the source. The complaint was that the transcription
// text was too big, which made the whole Session page taller than the viewport:
// the fixed transcript box was pushed off the bottom, the page scrolled instead
// of the box, and the newest captions could not be reached. The fix keeps the
// Session page inside one screen (the page never scrolls, only the transcript
// box does) and drops the transcript/cue/song text one step below the body size.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

const block = (html: string, selector: string): string => {
  // Grab the declarations of the first rule whose selector matches exactly.
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = html.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  if (!match) throw new Error(`no CSS rule for "${selector}"`);
  return match[1];
};

describe("Session page fits one screen (XERK-195)", () => {
  const html = readText("index.html");

  it("lays the Session page out as a full-height flex column", () => {
    // The page fills the content area and stacks its children, so the transcript
    // can take the leftover height rather than a fixed slice of the viewport.
    // Scoped with :not([hidden]) so it applies only while the Session page is up.
    const rule = block(html, "#page-session:not([hidden])");
    expect(rule).toMatch(/display:\s*flex/);
    expect(rule).toMatch(/flex-direction:\s*column/);
    expect(rule).toMatch(/height:\s*100%/);
  });

  it("lets the transcript box flex-fill and scroll instead of pushing off screen", () => {
    const rule = block(html, "#session-text.transcript");
    // Grows into the leftover column height and may shrink past its content...
    expect(rule).toMatch(/flex:\s*1/);
    expect(rule).toMatch(/min-height:\s*0/);
    // ...scrolling inside itself rather than the page.
    expect(rule).toMatch(/overflow-y:\s*auto/);
    // The old fixed height is gone — that was what let the cards push it off.
    expect(rule).not.toMatch(/max-height/);
  });

  it("makes the transcript, cue and song text a step smaller than the body", () => {
    // The literal ask: smaller text in the companion. The transcript rows inherit
    // the ul's size; the cue and song card bodies match it so the page reads as one.
    expect(block(html, "#session-text.transcript")).toMatch(/font-size:\s*var\(--text-sm\)/);
    expect(block(html, ".session-cue-body")).toMatch(/font-size:\s*var\(--text-sm\)/);
    expect(block(html, ".session-song-body")).toMatch(/font-size:\s*var\(--text-sm\)/);
  });
});
