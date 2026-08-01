import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level checks (the RN components aren't rendered in this jsdom suite,
// mirroring cue.test.ts / tabBar.test.ts). They guard the XERK-184 parity: the
// mobile synced-lyrics band mirrors the web SPA's LiveLyricsBand, drives its
// scroll off the SHARED client-core helpers, and owns the box in place of the
// cue band while a song plays.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("mobile synced-lyrics band (XERK-184)", () => {
  const lyrics = readText("src/ui/lyrics.tsx");
  const live = readText("src/screens/Live.tsx");

  it("exposes a LiveLyricsBand driven by the shared scroll helpers", () => {
    expect(lyrics).toContain("export function LiveLyricsBand(");
    // The scroll uses the SHARED client-core primitives, not a re-implementation,
    // so it stays in lockstep with the web band.
    expect(lyrics).toContain("currentLyricIndex");
    expect(lyrics).toContain("lyricWindow");
    // Title is "TITLE — ARTIST" (XERK-190).
    expect(lyrics).toContain("{song.title} — {song.artist}");
    // The current line is highlighted; empty lyrics fall back to a no-scroll marker.
    expect(lyrics).toContain("i === win.currentIndex ? styles.current : null");
  });

  it("recomputes the current line on a sub-second local tick", () => {
    expect(lyrics).toContain("setInterval");
    expect(lyrics).toMatch(/LYRIC_SCROLL_TICK_MS\s*=\s*250/);
  });

  it("mounts the lyrics band in place of the cue band while a song plays", () => {
    expect(live).toContain("LiveLyricsBand");
    // A live song owns the box: the cue band is only rendered when there is no song.
    expect(live).toContain("state.song ? (");
    expect(live).toContain("<LiveLyricsBand song={state.song} />");
  });
});

describe("mobile history renders recognized songs inline (XERK-184)", () => {
  const history = readText("src/screens/History.tsx");

  it("places songs on the transcript timeline like cues", () => {
    expect(history).toContain('kind: "song"');
    expect(history).toContain("conv.songs");
    // Rendered as "♪ TITLE — ARTIST" at the point it played (XERK-190).
    expect(history).toContain("{run.song.title} — {run.song.artist}");
    // Songs stay visible even with the cue toggle off (they aren't cues).
    expect(history).toContain('it.kind !== "cue" || showCues');
  });
});
