/** Ported from upstream `packages/client-core` captureSession song/cue slices. */

import { describe, expect, it } from "bun:test";

import {
  CUE_TTL_MS,
  type LiveSong,
  cueCountdownLabel,
  cueSecondsLeft,
  currentLyricIndex,
  lyricWindow,
  songPositionMs,
} from "./live";

const song = (lines: Array<[number, string]>, anchorAt = 0, anchorOffsetMs = 0): LiveSong => ({
  id: "s",
  title: "T",
  artist: "A",
  lines: lines.map(([atMs, text]) => ({ atMs, text })),
  anchorAt,
  anchorOffsetMs,
});

describe("cue countdown", () => {
  it("derives whole seconds left from the elapsed time", () => {
    expect(cueSecondsLeft(0)).toBe(CUE_TTL_MS / 1000);
    expect(cueSecondsLeft(2500)).toBe(8);
    expect(cueSecondsLeft(CUE_TTL_MS)).toBe(0);
    expect(cueSecondsLeft(CUE_TTL_MS + 5000)).toBe(0); // never negative
  });

  it("labels as Ns", () => {
    expect(cueCountdownLabel(7)).toBe("7s");
  });
});

describe("songPositionMs", () => {
  it("advances with the wall clock from the anchor", () => {
    const s = song([], 1000, 30000);
    expect(songPositionMs(s, 1000)).toBe(30000);
    expect(songPositionMs(s, 6000)).toBe(35000);
  });

  it("never goes negative", () => {
    expect(songPositionMs(song([], 5000, 0), 1000)).toBe(0);
  });
});

describe("currentLyricIndex", () => {
  const s = song(
    [
      [0, "zero"],
      [10000, "one"],
      [20000, "two"],
    ],
    0,
    0,
  );

  it("is -1 before the first line and tracks the last reached line", () => {
    expect(currentLyricIndex(song([[5000, "later"]], 0, 0), 0)).toBe(-1);
    expect(currentLyricIndex(s, 0)).toBe(0);
    expect(currentLyricIndex(s, 15000)).toBe(1);
    expect(currentLyricIndex(s, 99999)).toBe(2);
  });
});

describe("lyricWindow", () => {
  const lines = Array.from({ length: 10 }, (_, i) => ({ atMs: i * 1000, text: `line ${i}` }));

  it("is empty for empty lyrics", () => {
    expect(lyricWindow([], 3)).toEqual({ lines: [], currentIndex: -1 });
  });

  it("shows the opening lines unhighlighted before the song starts", () => {
    const win = lyricWindow(lines, -1);
    expect(win.currentIndex).toBe(-1);
    expect(win.lines.map((l) => l.text)).toEqual(["line 0", "line 1", "line 2", "line 3"]);
  });

  it("pins the current line one row from the top mid-song", () => {
    const win = lyricWindow(lines, 5);
    expect(win.lines.map((l) => l.text)).toEqual(["line 4", "line 5", "line 6", "line 7"]);
    expect(win.currentIndex).toBe(1);
  });

  it("clamps at both ends so the window never runs past the lyrics", () => {
    const start = lyricWindow(lines, 0);
    expect(start.lines[0].text).toBe("line 0");
    expect(start.currentIndex).toBe(0);
    const end = lyricWindow(lines, 9);
    expect(end.lines.map((l) => l.text)).toEqual(["line 6", "line 7", "line 8", "line 9"]);
    expect(end.currentIndex).toBe(3);
  });
});
