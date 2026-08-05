/**
 * Lens HUD layout for the 576×288 G2 canvas — the miniapp counterpart of the
 * upstream `even/src/lens/layout.ts`, rebuilt on `session.display.render`
 * scene elements instead of Even Hub LVGL containers.
 *
 *   ┌──────────────────────┬────────┐  y=0
 *   │ status line          │12:59 PM│  h=40  (1 line; clock whenever signed in)
 *   ├──────────────────────┴────────┤  y=40
 *   │ caption band (live            │  h=248 (CAPTION_LINES whole 40px lines
 *   │ transcript)                   │        render; the trailing 8px stay
 *   └───────────────────────────────┘  unused)
 *
 * Wrapping is pixel-measured with the vendored display-utils G2 profile (G1
 * glyph tables, 40px lines), a touch narrower than the real canvas so any
 * residual drift between measured and rendered wrap errs toward trimming one
 * line too early — never toward a clipped line.
 *
 * Everything here is pure (no session), so it unit-tests under `bun test`.
 */

import { G2_PROFILE, TextMeasurer, TextWrapper } from "../vendor/display-utils";

export const SCREEN_W = 576;
export const SCREEN_H = 288;
/** Hardware-calibrated G2 line height (see vendor profiles/g2.ts). */
export const LINE_H = G2_PROFILE.lineHeightPx ?? 40;

// The clock band, top-right. The widest 12-hour time ("12:59 PM") measures
// 82px in the G1/G2 glyph tables; 96px keeps a small margin off the right edge.
export const CLOCK_W = 96;

// Measure wrapping a touch narrower than the real band (upstream layout.ts).
export const MEASURE_SAFETY_PX = 8;
export const CAPTION_WRAP_W = SCREEN_W - MEASURE_SAFETY_PX;

/** The caption band: everything under the status/clock line. */
export const CAPTION_Y = LINE_H;
export const CAPTION_H = SCREEN_H - LINE_H;
/** How many whole lines fit the caption band — content is trimmed to this. */
export const CAPTION_LINES = Math.floor(CAPTION_H / LINE_H);

/** Stable element ids so successive renders update in place (flicker-free). */
export const ELEMENT_IDS = {
  status: "status",
  clock: "clock",
  caption: "caption",
} as const;

export const SIGN_IN_PROMPT = "Not signed in — open the Tenir app on your phone to sign in.";
export const IDLE_PROMPT = "Tap to start a new session.";

const measurer = new TextMeasurer(G2_PROFILE);
const wrapper = new TextWrapper(measurer, {
  // Greedy word wrap, hyphenating only a word longer than a whole row —
  // the closest mode to upstream's wrapLines.
  breakMode: "word",
  preserveNewlines: true,
});

/**
 * Split text into the physical rows the band renders: explicit newlines are
 * respected, longer paragraphs greedy-wrapped at word boundaries
 * (pixel-measured with the G2 glyph tables).
 */
export function wrapLines(text: string, maxWidth = CAPTION_WRAP_W): string[] {
  return wrapper.wrap(text, {
    maxWidthPx: maxWidth,
    maxLines: Number.MAX_SAFE_INTEGER,
    maxBytes: Number.MAX_SAFE_INTEGER,
  }).lines;
}

/**
 * The caption band as exactly `maxLines` physical rows: the LAST rows of the
 * wrapped transcript, top-padded with empty rows so new text keeps arriving at
 * the BOTTOM of the band. Old text simply falls off the top.
 */
export function fitCaptionRows(
  text: string,
  maxLines = CAPTION_LINES,
  maxWidth = CAPTION_WRAP_W,
): string[] {
  const wrapped = wrapLines(text, maxWidth);
  const kept = wrapped.length > maxLines ? wrapped.slice(-maxLines) : wrapped;
  return [...Array<string>(maxLines - kept.length).fill(""), ...kept];
}

/** `fitCaptionRows` joined for the caption element (empty text stays empty). */
export function fitCaption(
  text: string,
  maxLines = CAPTION_LINES,
  maxWidth = CAPTION_WRAP_W,
): string {
  if (!text) return "";
  return fitCaptionRows(text, maxLines, maxWidth).join("\n");
}

/** The animated activity dots: 1 → 2 → 3 dots, cycling with the ticker. */
export function dots(tick: number): string {
  return ".".repeat((tick % 3) + 1);
}

/** The top-right clock text: 12-hour h:MM AM/PM. */
export function clockText(date: Date): string {
  const h24 = date.getHours();
  const h = h24 % 12 || 12; // 0 and 12 both show as 12
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${h}:${mm} ${h24 < 12 ? "AM" : "PM"}`;
}

/**
 * The status line, honest about connectivity: outside a session the lens says
 * it is ready rather than pretending to listen, and a dropped or unreachable
 * server is named rather than hidden. While recording with an open socket it
 * reads "listening" with dots that move with `tick`.
 */
export function statusLine(
  state: { recording: boolean; connection: "connecting" | "open" | "closed" },
  tick = 0,
): string {
  if (!state.recording) return "ready";
  if (state.connection === "connecting") return "connecting to server…";
  if (state.connection === "closed") return "server unreachable — retrying";
  return `listening${dots(tick)}`;
}

/** One frame of the lens HUD as scene-element text contents. */
export interface HudFrame {
  status: string;
  clock: string;
  caption: string;
}

/**
 * The scene for a HUD frame: three text elements with stable ids. Kept here
 * (rather than in the controller) so the geometry is testable alongside the
 * fitting logic.
 */
export function hudElements(frame: HudFrame): Array<{
  type: "text";
  id: string;
  box: { x: number; y: number; w: number; h: number };
  text: string;
}> {
  return [
    {
      type: "text",
      id: ELEMENT_IDS.status,
      box: { x: 0, y: 0, w: SCREEN_W - CLOCK_W, h: LINE_H },
      text: frame.status,
    },
    {
      type: "text",
      id: ELEMENT_IDS.clock,
      box: { x: SCREEN_W - CLOCK_W, y: 0, w: CLOCK_W, h: LINE_H },
      text: frame.clock,
    },
    {
      type: "text",
      id: ELEMENT_IDS.caption,
      box: { x: 0, y: CAPTION_Y, w: SCREEN_W, h: CAPTION_H },
      text: frame.caption,
    },
  ];
}
