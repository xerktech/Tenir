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

import { LYRIC_LINES_BEFORE, cueCountdownLabel, type LyricWindow } from "../core/live";
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
/** How many whole lines fit the caption band — content is trimmed to this. */
export const CAPTION_LINES = Math.floor((SCREEN_H - LINE_H) / LINE_H);
/**
 * The caption band is EXACTLY that many lines tall (upstream layout.ts): a
 * taller band leaves a half-line slot at the bottom — one mis-wrapped line
 * ends half-visible in it and the host grows a scroll bar to reach the rest.
 */
export const CAPTION_H = CAPTION_LINES * LINE_H;

/** Stable element ids so successive renders update in place (flicker-free). */
export const ELEMENT_IDS = {
  status: "status",
  clock: "clock",
  caption: "caption",
  popupBox: "popup-box",
  popupText: "popup-text",
} as const;

// ---- the shared popup box (upstream layout.ts, adapted to the 40px grid) ----
// One bordered full-width strip across the top of the screen carries every
// popup: the double-tap Continue/Exit menu, a private-context cue (XERK-112),
// a translation run (XERK-160), and a recognized song's lyrics (XERK-184).
// Whatever caption rows it covers are masked while it is up (occludedCaption),
// and the status/clock line is blanked, so nothing shows through the box.
export const CUE_BORDER = 3;
export const CUE_PAD = 16;
/** A cue box body is up to this many rows (XERK-119: shorter cues shrink it). */
export const CUE_BODY_LINES = 4;
/** The translation box shows one MORE body row than a cue (XERK-176). */
export const TRANSLATION_BODY_LINES = 5;
/** The song box's lyric window: 4 rows, matching a cue's body (XERK-184). */
export const SONG_BODY_LINES = 4;
export const CUE_ROWS = 1 + CUE_BODY_LINES;
export const TRANSLATION_ROWS = 1 + TRANSLATION_BODY_LINES;
export const SONG_ROWS = 1 + SONG_BODY_LINES;
/** The box height for a given row count: content rows + symmetric pad/border.
 *  At pad+border = 19 the 2-row menu is 118px — inside the 3rd 40px line's
 *  120px boundary, so it masks exactly the status line and two caption rows,
 *  the same whole-row rule upstream sized its 80px menu to (layout.ts). */
export function cueHeight(rows: number): number {
  return rows * LINE_H + 2 * (CUE_PAD + CUE_BORDER);
}
export const MENU_H = cueHeight(2);
/** The width popup text wraps at: the box interior minus the safety margin. */
export const CUE_TEXT_W = SCREEN_W - 2 * (CUE_PAD + CUE_BORDER) - MEASURE_SAFETY_PX;
/** The caption rows (0-based within the band) a box of `height` px masks. */
export function cueRowRangeFor(height: number): [number, number] {
  const first = 0;
  const last = Math.ceil((height - LINE_H) / LINE_H) - 1;
  return [first, Math.max(first, Math.min(last, CAPTION_LINES - 1))];
}

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

// ---- popup text builders (upstream even/src/lens/layout.ts) -----------------

/** Pixel width of a string in the G2 glyph tables. */
function getTextWidth(text: string): number {
  return measurer.measureText(text);
}

/** A cue-shaped card: what the cue, translation and song boxes all render. */
export interface CueCard {
  title: string;
  body: string;
  /** Live-source attribution (XERK-120) — phone-only, never on the lens. */
  source?: string;
}

/** The in-session popup's two choices: Continue is the default, on top. */
export type MenuChoice = "continue" | "exit";

/**
 * The in-session popup's rows: Continue on top (the default) with Exit session
 * below, the highlighted row marked with "›". Swiping moves the highlight; a
 * single tap confirms it.
 */
export function menuText(selected: MenuChoice): string {
  const row = (choice: MenuChoice, label: string) =>
    `${selected === choice ? "›" : " "} ${label}`;
  return `${row("continue", "Continue")}\n${row("exit", "Exit session")}`;
}

/**
 * Lay a left and a right label out on one popup row, the right one flush to the
 * row's right edge (XERK-110). The HUD has no alignment control — a row is one
 * string — so the gap is spelled out in spaces, sized by measuring them against
 * the slack the two labels leave.
 */
function rowWithRightEdge(left: string, right: string, width: number): string {
  if (!right) return left;
  const spaceWidth = getTextWidth(" ");
  if (spaceWidth <= 0) return `${left} ${right}`;
  const slack = width - getTextWidth(left) - getTextWidth(right);
  let spaces = Math.max(1, Math.floor(slack / spaceWidth));
  while (spaces > 1 && getTextWidth(`${left}${" ".repeat(spaces)}${right}`) > width) spaces--;
  return `${left}${" ".repeat(spaces)}${right}`;
}

/** The cue body wrapped into physical rows at the box's interior width. */
export function cueBodyLines(body: string): string[] {
  return wrapLines(body, CUE_TEXT_W);
}

/**
 * The pinned title row of a cue box (XERK-112): the title wrapped to a single
 * row, with the countdown to auto-dismissal (XERK-110) flush to the right edge.
 * The title wraps to whatever width the countdown leaves, so a long one is
 * trimmed instead of pushing the row over the edge; omitted (no countdown) the
 * row is the title alone.
 */
export function cueTitleLine(card: CueCard, secondsLeft?: number): string {
  const countdown = secondsLeft == null ? "" : cueCountdownLabel(secondsLeft);
  const reserved = countdown ? getTextWidth(countdown) + getTextWidth(" ") : 0;
  const titleLine = wrapLines(card.title, CUE_TEXT_W - reserved)[0] ?? card.title;
  return rowWithRightEdge(titleLine, countdown, CUE_TEXT_W);
}

/**
 * The song box's title (XERK-190): "SONG NAME — ARTIST", the same form the
 * web/mobile lyric cards use. `cueTitleLine` wraps/trims it to the title row
 * (with no countdown — a song box has none, it clears on `song.done`).
 */
export function songTitle(artist: string, title: string): string {
  return `${title} — ${artist}`;
}

// The marker column's width: every `songBody` row starts with a two-character
// prefix ("> " or "  "), so the lyric text itself wraps at the box interior
// minus the wider prefix — keeping wrap points identical whichever line the
// marker sits on.
const SONG_PREFIX_W = Math.max(getTextWidth("> "), getTextWidth("  "));

/**
 * The song box's body rows (XERK-189): the lyric window with the line being
 * sung marked "> " — the HUD renders one weight, so the marker stands in for
 * the bolded current line on web/mobile. Every other row is indented so the
 * lyrics stay in one column. Before the song reaches its first line no row is
 * marked; a song with no synced lyrics is the quiet ♪ ♪ ♪ marker.
 *
 * The body is FITTED to at most `maxLines` physical rows (XERK-191) so
 * repaints can never overflow the box: the current line's rows all render,
 * already-sung context keeps its LYRIC_LINES_BEFORE-row slot (growing to
 * absorb what the upcoming rows can't fill), and upcoming rows fill the rest,
 * clipped bottom-first.
 */
export function songBody(win: LyricWindow, maxLines = SONG_BODY_LINES): string {
  if (win.lines.length === 0) return "♪ ♪ ♪";
  const wrapped = win.lines.map((l, i) =>
    wrapLines(l.text || "♪", CUE_TEXT_W - SONG_PREFIX_W).map(
      (row, j) => `${i === win.currentIndex && j === 0 ? ">" : " "} ${row}`,
    ),
  );
  if (win.currentIndex < 0) {
    // Not started: the opening rows, top-first, capped to the box.
    return wrapped.flat().slice(0, maxLines).join("\n");
  }
  const above = wrapped.slice(0, win.currentIndex).flat();
  const current = wrapped[win.currentIndex];
  const below = wrapped.slice(win.currentIndex + 1).flat();
  // A single line wrapping past the whole box cannot fit by definition — clip
  // it as the last resort (its marker row first).
  if (current.length >= maxLines) return current.slice(0, maxLines).join("\n");
  const remaining = maxLines - current.length;
  const aboveTake = Math.min(
    above.length,
    Math.max(remaining - below.length, Math.min(remaining, LYRIC_LINES_BEFORE)),
  );
  return [
    ...above.slice(above.length - aboveTake),
    ...current,
    ...below.slice(0, remaining - aboveTake),
  ].join("\n");
}

/**
 * A streaming body tailed to the box's last `maxLines` rows (XERK-172): the
 * translation box accumulates turn after turn, so it keeps the most recent
 * rows and lets older ones fall off the top — a new turn always arrives at the
 * BOTTOM. A body still short of `maxLines` is returned whole (the box stays
 * short, XERK-119).
 */
export function tailCueBody(body: string, maxLines = CUE_BODY_LINES): string {
  const rows = cueBodyLines(body);
  return (rows.length > maxLines ? rows.slice(-maxLines) : rows).join("\n");
}

/**
 * The caption band while a popup is up: the same fitted rows, but the rows the
 * popup box touches are masked to "" — exactly what an opaque popup would
 * hide. Rows above and below keep flowing, so the conversation visibly
 * continues around the box and nothing renders underneath it.
 */
export function occludedCaption(text: string, firstRow: number, lastRow: number): string {
  if (!text) return "";
  const rows = fitCaptionRows(text);
  for (let r = firstRow; r <= lastRow && r < rows.length; r++) rows[r] = "";
  return rows.join("\n");
}

/**
 * One popup box's render-ready content: the rows it draws (title first for the
 * cue-shaped boxes; the two menu rows for the menu) — the box is sized to
 * exactly these rows (XERK-119: a shorter body makes a shorter box).
 */
export interface HudPopup {
  /** All content rows, joined by newline. */
  text: string;
  /** The physical row count `text` carries (drives the box height). */
  rows: number;
}

/** A popup from a cue-shaped card: pinned title row over the body rows. */
export function cardPopup(
  card: CueCard,
  opts: { secondsLeft?: number; bodyLines?: number } = {},
): HudPopup {
  const maxLines = opts.bodyLines ?? CUE_BODY_LINES;
  // Read-from-the-top body, capped to the box (the scene API has no
  // host-native scroll to hand overflow to — a documented platform exception).
  const body = cueBodyLines(card.body).slice(0, maxLines);
  const text = [cueTitleLine(card, opts.secondsLeft), ...body].join("\n");
  return { text, rows: 1 + body.length };
}

/** A popup whose body is already fitted rows (the song box). */
export function fittedPopup(title: string, fittedBody: string): HudPopup {
  const text = `${title}\n${fittedBody}`;
  return { text, rows: 1 + fittedBody.split("\n").length };
}

/** The Continue / Exit session menu as a popup. */
export function menuPopup(selected: MenuChoice): HudPopup {
  return { text: menuText(selected), rows: 2 };
}

/** One frame of the lens HUD as scene-element text contents. */
export interface HudFrame {
  status: string;
  clock: string;
  caption: string;
  /** The popup box on top (menu > song > translation > cue), if any. */
  popup?: HudPopup | null;
}

/** One element of the HUD scene (the slice of the SDK's RenderElement we emit). */
export type HudElement =
  | {
      type: "text";
      id: string;
      box: { x: number; y: number; w: number; h: number };
      text: string;
      style?: { border?: number; radius?: number };
    }
  | {
      type: "rect";
      id: string;
      box: { x: number; y: number; w: number; h: number };
      style?: { border?: number; radius?: number };
    };

/**
 * The scene for a HUD frame: three text elements with stable ids, plus — when
 * a popup is up — the bordered box and its content on top (the host draws
 * elements in order, so the box overlays the caption band, whose covered rows
 * the controller has already masked via `occludedCaption`). Kept here (rather
 * than in the controller) so the geometry is testable alongside the fitting
 * logic.
 */
export function hudElements(frame: HudFrame): HudElement[] {
  const elements: HudElement[] = [
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
  if (frame.popup) {
    const height = cueHeight(frame.popup.rows);
    elements.push(
      {
        type: "rect",
        id: ELEMENT_IDS.popupBox,
        box: { x: 0, y: 0, w: SCREEN_W, h: height },
        style: { border: CUE_BORDER },
      },
      {
        type: "text",
        id: ELEMENT_IDS.popupText,
        box: {
          x: CUE_PAD + CUE_BORDER,
          y: CUE_PAD + CUE_BORDER,
          w: SCREEN_W - 2 * (CUE_PAD + CUE_BORDER),
          h: frame.popup.rows * LINE_H,
        },
        text: frame.popup.text,
      },
    );
  }
  return elements;
}
