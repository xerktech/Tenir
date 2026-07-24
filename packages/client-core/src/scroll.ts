/**
 * Live-transcript auto-scroll geometry (XERK-103), shared by every frontend so
 * the web SPA, the Android app and the Even phone companion follow the newest
 * caption identically.
 *
 * The running conversation lives in its own bounded, internally-scrolling box
 * with the cue band pinned above it: a growing session must never scroll the
 * whole page and carry the cues out of view. That box "sticks" to the bottom —
 * following new text — only while the viewer is already there; once they scroll
 * up to re-read, fresh captions stop yanking them back down until they return
 * to the bottom themselves.
 *
 * Pure geometry (no DOM/RN types) so it unit-tests without a renderer and reads
 * the same whether fed a browser element's `scrollTop`/`scrollHeight`/
 * `clientHeight` or React Native's `contentOffset`/`contentSize`/
 * `layoutMeasurement`.
 */

export interface ScrollGeometry {
  /** Current scroll offset from the top of the content. */
  scrollTop: number;
  /** Total height of the scrollable content. */
  scrollHeight: number;
  /** The visible height of the scroll box (its viewport). */
  clientHeight: number;
}

/**
 * Slack, in pixels, within which the box still counts as "at the bottom".
 * A little tolerance keeps the feed pinned through sub-pixel rounding and the
 * brief gap while a new line is measured.
 */
export const STICK_THRESHOLD_PX = 24;

/**
 * True when the box is scrolled to (or within `threshold` px of) the bottom —
 * i.e. the viewer is following the live feed, so new text should keep it pinned
 * to the bottom. False once they have scrolled up to read earlier captions.
 */
export function isPinnedToBottom(g: ScrollGeometry, threshold = STICK_THRESHOLD_PX): boolean {
  return g.scrollHeight - g.scrollTop - g.clientHeight <= threshold;
}
