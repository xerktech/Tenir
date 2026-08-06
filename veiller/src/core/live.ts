/**
 * Live-surface helpers shared by the lens HUD and the phone mirror — ported
 * from upstream `packages/client-core/src/captureSession.ts` (the pure slice:
 * lyric windowing for a recognized song, XERK-184, and the cue countdown,
 * XERK-110). Kept environment-free (no DOM, no `URL`, no timers) so both the
 * background JSContext and the WebView bundle it and unit tests run under
 * `bun test`.
 */

import type { LyricLine } from "./messages";

// A cue box stays up this long, then is auto-dismissed (XERK-81).
export const CUE_TTL_MS = 10000;

// How many lyric lines the box shows around the current one (XERK-184): one
// already-sung line for context, the current line, and two upcoming — four rows,
// matching the cue box. Shared so every frontend windows the lyrics identically.
export const LYRIC_LINES_BEFORE = 1;
export const LYRIC_LINES_AFTER = 2;

/**
 * A song recognized playing, whose time-synced lyrics auto-scroll (XERK-184).
 * The scroll is driven client-side from an anchor: at wall-clock `anchorAt`
 * (ms epoch, when this client applied the anchor) the song was at
 * `anchorOffsetMs` into the track, so the position at any later time `now` is
 * `anchorOffsetMs + (now - anchorAt)`. `song.sync` refreshes the anchor to
 * correct drift; the local clock carries the scroll smoothly between syncs.
 */
export interface LiveSong {
  id: string;
  title: string;
  artist: string;
  /** The full time-synced lyrics (LRC), ordered by song time. Empty = show the
   *  title with no scroll (no synced lyrics were found). */
  lines: LyricLine[];
  anchorAt: number;
  anchorOffsetMs: number;
  durationMs?: number;
}

/** Seconds left before a cue shown `elapsedMs` ago is auto-dismissed (XERK-110). */
export function cueSecondsLeft(elapsedMs: number): number {
  return Math.max(0, Math.ceil((CUE_TTL_MS - elapsedMs) / 1000));
}

/** The countdown as it is painted in a cue's top-right corner, e.g. `"7s"`. */
export function cueCountdownLabel(secondsLeft: number): string {
  return `${secondsLeft}s`;
}

/** The song's play position (ms) at wall-clock `now`, from its scroll anchor
 *  (XERK-184). Reads off the real clock, so a throttled or backgrounded client
 *  resyncs to the truth on the next tick rather than drifting. */
export function songPositionMs(
  song: Pick<LiveSong, "anchorAt" | "anchorOffsetMs">,
  now: number,
): number {
  return Math.max(0, song.anchorOffsetMs + (now - song.anchorAt));
}

/**
 * Index of the currently-sung lyric line at wall-clock `now` — the last line
 * whose (song-time) `atMs` has been reached, or -1 before the first line. Pure
 * and shared, so the lens box and the phone card scroll identically.
 */
export function currentLyricIndex(song: LiveSong, now: number): number {
  const pos = songPositionMs(song, now);
  let idx = -1;
  for (let i = 0; i < song.lines.length; i++) {
    if (song.lines[i].atMs <= pos) idx = i;
    else break; // lines are time-ordered
  }
  return idx;
}

/** A window of lyric lines around the current one, for the fixed-height box. */
export interface LyricWindow {
  /** The visible slice of lines. */
  lines: LyricLine[];
  /** Index of the current line WITHIN `lines`, or -1 when none is current yet
   *  (the song hasn't reached the first line, or lyrics are empty). */
  currentIndex: number;
}

/**
 * The slice of lyrics to render around `index` (from `currentLyricIndex`):
 * `before` already-sung lines for context, the current line, and `after`
 * upcoming — the auto-scroll "window". Before the song reaches its first line
 * (`index < 0`) it shows the opening lines with nothing highlighted. Pure and
 * shared so every frontend scrolls the same rows in lockstep.
 */
export function lyricWindow(
  lines: LyricLine[],
  index: number,
  before: number = LYRIC_LINES_BEFORE,
  after: number = LYRIC_LINES_AFTER,
): LyricWindow {
  const size = before + 1 + after;
  if (lines.length === 0) return { lines: [], currentIndex: -1 };
  if (index < 0) {
    // Not started: show the opening lines, nothing highlighted.
    return { lines: lines.slice(0, size), currentIndex: -1 };
  }
  // Keep the current line `before` rows from the top, clamped to the ends so the
  // window never runs past the last line or before the first.
  let start = index - before;
  if (start < 0) start = 0;
  if (start > Math.max(0, lines.length - size)) start = Math.max(0, lines.length - size);
  return { lines: lines.slice(start, start + size), currentIndex: index - start };
}
