/**
 * Lens layout for the 576x288 G2 HUD.
 *
 *   ┌──────────────────────┬────────┐  y=0
 *   │ status line (tiny)   │12:59 PM│  h=27  (1 line; clock whenever signed in)
 *   ├──────────────────────┴────────┤  y=27
 *   │ caption band  (live           │  h=243 (exactly CAPTION_LINES lines —
 *   │ transcript)                   │  no half-line slot)
 *   └───────────────────────────────┘  y=270 (the last 18px stay unused)
 *
 * Exactly ONE container captures input per page — and it is NEVER a visible
 * one (XERK-85 feedback: the OS plays its scroll animation on whatever
 * container captures a scroll gesture — it hit the session text first, then
 * the clock when capture moved there). Every page therefore carries an
 * INVISIBLE full-band "touch" overlay (content: a single space) at the same
 * geometry as the caption band: it captures every gesture, and the OS bounce
 * animation moves content nobody can see. Live text updates go through
 * `textContainerUpgrade` (flicker-free), never a rebuild.
 *
 * XERK-85: while a session is recording the status line reads "listening" with
 * animated dots and the clock container shows the current time top-right. The
 * caption band is trimmed to the tail that FITS the band (`fitCaption`) so the
 * host never has overflow to scroll — old text simply falls off the top. The
 * band's height is an exact multiple of the line height and padding is pinned
 * to 0, so a fitted transcript can never end on a half-visible line (which
 * would make the host grow a scroll bar for the clipped remainder).
 */

import {
  CreateStartUpPageContainer,
  type EvenAppBridge,
  RebuildPageContainer,
  TextContainerProperty,
  TextContainerUpgrade,
} from "@evenrealities/even_hub_sdk";
import { getTextWidth } from "@evenrealities/pretext";

import { cueCountdownLabel } from "@tenir/client-core";

export const SCREEN_W = 576;
export const SCREEN_H = 288;
export const LINE_H = 27; // baked-in LVGL line height

// The clock band, top-right. The widest 12-hour time ("12:59 PM") renders 82px
// in the EvenHub font (digits are tabular), so 96px keeps a small breathing
// margin off the right edge.
export const CLOCK_W = 96;

// How many whole lines fit the caption band. Content is always trimmed to this,
// so the band never overflows — and an overflow-free container has nothing for
// the host to scroll (XERK-85: no scrolling while recording).
export const CAPTION_LINES = Math.floor((SCREEN_H - LINE_H) / LINE_H);
// The caption band is EXACTLY that many lines tall. A taller band (the raw
// 261px remainder) leaves a half-line slot at the bottom: one mis-wrapped line
// ends half-visible in it and the host grows a scroll bar to reach the rest.
export const CAPTION_H = CAPTION_LINES * LINE_H;
// Measure wrapping a touch narrower than the real band. pretext mirrors the
// LVGL wrapper, but any residual drift between measured and rendered wrap
// must err toward trimming one line too early (invisible) — never toward one
// line too many (a clipped line + scroll bar).
export const MEASURE_SAFETY_PX = 8;

export const CONTAINER = {
  status: { id: 1, name: "status" },
  caption: { id: 2, name: "caption" },
  clock: { id: 3, name: "clock" },
  menu: { id: 4, name: "menu" }, // the double-tap popup box, and a cue's pinned title row
  touch: { id: 5, name: "touch" }, // invisible full-band gesture-capture overlay
  cueBody: { id: 6, name: "cueBody" }, // a cue's scrolling body (host-native scroll, XERK-133)
} as const;

// ---- the double-tap popup box (XERK-85) -------------------------------------
// A bordered text container overlaid on the page via `rebuildPageContainer`
// (the SDK's sanctioned runtime page change): a FULL-WIDTH strip from the top
// of the screen. Its two 27px option rows get symmetric top/bottom padding,
// which makes the strip 80px tall — covering the status/clock line and the
// first two transcript rows, but ending INSIDE the third row's 81px boundary
// so it never costs another line. Everything it covers is blanked while it is
// up (the status + clock containers write "", occludedCaption masks the
// covered caption rows), so nothing shows through it while the rest of the
// transcript keeps flowing below — visually an opaque popup on top of the
// live conversation.
export const MENU_BORDER = 2;
// The biggest symmetric padding that keeps the strip within three lines:
// 2*LINE_H content + 2*(pad+border) <= 3*LINE_H  =>  pad <= 13.5 - border.
export const MENU_PAD = 11;
export const MENU_W = SCREEN_W;
export const MENU_H = 2 * LINE_H + 2 * (MENU_PAD + MENU_BORDER);
export const MENU_X = 0;
export const MENU_Y = 0;
// The caption-band rows the box touches (0-based within the band): masked to
// "" while the popup is up so nothing renders underneath the box. (The box
// also covers the status line above the band — blanked separately.)
// The width the popup's own rows get: the strip minus its border and padding
// on both sides, measured with the same safety margin as the caption band.
// Text laid out for the box (menu rows, a cue's title/detail) must wrap at THIS
// width — the full screen width would wrap late and spill the box onto a third
// line it has no room for.
export const MENU_TEXT_W = MENU_W - 2 * (MENU_PAD + MENU_BORDER) - MEASURE_SAFETY_PX;
export const MENU_ROW_FIRST = Math.max(0, Math.floor((MENU_Y - LINE_H) / LINE_H));
export const MENU_ROW_LAST = Math.ceil((MENU_Y + MENU_H - LINE_H) / LINE_H) - 1;

// ---- the private-context cue box (XERK-112, XERK-119, XERK-133) -------------
// The cue box is a bordered strip like the menu, but taller: a pinned title row
// over up to CUE_BODY_LINES body rows, so a cue reads at a glance instead of
// being clipped to one line. The body lives in its OWN container inside the box
// (`cueBodyBox`): when the wrapped body runs past CUE_BODY_LINES rows that
// container's content overflows it and the HOST scrolls it with its own native
// scroll bar (XERK-133) — the body container is the one event-capture container
// while a cue is up, so a swipe drives the host's scroll directly. Splitting the
// body out this way keeps the title (and its live countdown) pinned: the ticker
// repaints only the title, never the body, so a countdown tick can't yank the
// host's scroll position back to the top. The box is sized to the cue it holds
// (XERK-119): a body shorter than CUE_BODY_LINES makes a shorter box — blank
// rows would just mask live transcript rows below it — while a body that fills
// or overflows gets the full CUE_BODY_LINES-row box.
export const CUE_BODY_LINES = 4;
// The translation box (XERK-160) reuses this same geometry but shows one MORE
// body row than a cue (XERK-176): five, so a non-English run keeps more of its
// recent turns on screen at once. The box functions take a `maxLines` the
// translation call sites pass this for; cues keep the CUE_BODY_LINES default.
// Its full-height box is six rows (title + five body) — 188px, still
// within the 7th transcript row's 189px boundary, so it too masks only whole
// caption rows (see `cueHeight`).
export const TRANSLATION_BODY_LINES = 5;
// The synced-lyric box (XERK-184) reuses this same geometry to auto-scroll a
// recognized song's lyrics. Its body is a fixed window of lyric lines around the
// one being sung — one already-sung line for context, the current line, and two
// upcoming (LYRIC_LINES_BEFORE + 1 + LYRIC_LINES_AFTER in client-core) — four
// rows, matching a cue's body. The box functions take a `maxLines` the song call
// sites pass this for, exactly as the translation box passes its own count.
export const SONG_BODY_LINES = 4;
// The MOST rows the box is ever tall: the title over a full CUE_BODY_LINES-row
// body window. A shorter cue makes a shorter box (see `cueBox`/`cueRows`).
export const CUE_ROWS = 1 + CUE_BODY_LINES;
// The translation box's full-height row count (XERK-176): title + five body.
export const TRANSLATION_ROWS = 1 + TRANSLATION_BODY_LINES;
// The song box's full-height row count (XERK-184): title + four lyric rows.
export const SONG_ROWS = 1 + SONG_BODY_LINES;
export const CUE_BORDER = MENU_BORDER;
export const CUE_PAD = MENU_PAD;
export const CUE_W = SCREEN_W;
export const CUE_X = 0;
export const CUE_Y = 0;
// The box height for a given row count: content rows + symmetric padding +
// border. At the CUE_ROWS max (5 rows), with pad+border = 13, this is 161px —
// within the 6th transcript row's 162px boundary, so a full box occupies five
// whole caption rows and never grows a half-line the host would scroll. The
// taller TRANSLATION_ROWS box (6 rows, XERK-176) is 188px, still shy of the
// 7th row's 189px boundary, so it masks six whole rows the same way. A shorter
// box lands on a whole-row boundary the same way (every row is LINE_H and
// padding is fixed).
export function cueHeight(rows: number): number {
  return rows * LINE_H + 2 * (CUE_PAD + CUE_BORDER);
}
// The MAX box height (a full CUE_ROWS-row box), kept for reference/tests.
export const CUE_H = cueHeight(CUE_ROWS);
export const CUE_TEXT_W = CUE_W - 2 * (CUE_PAD + CUE_BORDER) - MEASURE_SAFETY_PX;
// The caption rows a box of the given height covers: the range masked while it
// is up (0-based within the band). first is always the top of the band; last is
// derived from the box height, so a shorter box masks fewer rows (XERK-119).
export function cueRowRangeFor(height: number): [number, number] {
  const first = Math.max(0, Math.floor((CUE_Y - LINE_H) / LINE_H));
  const last = Math.ceil((CUE_Y + height - LINE_H) / LINE_H) - 1;
  return [first, last];
}
// The rows a full-height cue box covers, kept for reference/tests.
export const [CUE_ROW_FIRST, CUE_ROW_LAST] = cueRowRangeFor(CUE_H);

/** The text every base container carries when a page is (re)built. */
export interface PageContents {
  status: string;
  caption: string;
  clock: string;
}

/**
 * The four always-present containers: status line, caption band, clock, and
 * the invisible full-band "touch" overlay. paddingLength/borderWidth are
 * pinned to 0 on each so the width the host wraps at IS the width fitCaption
 * measures at — an unnoticed host default padding would wrap earlier than
 * measured and overflow the band.
 *
 * The touch overlay is the event-capture container on every page EXCEPT while
 * a cue is up (XERK-85): the OS plays its scroll animation on whatever container
 * captures a scroll gesture, so the captured one must render nothing anybody can
 * see. The overlay shares the caption band's exact geometry (the capture target
 * every device-validated build used) but its content is a single space —
 * gestures land on it, and the bounce animation moves invisible content. A cue
 * page passes `captureTouch: false` and hands capture to the scrolling cue-body
 * container instead (XERK-133), so the host scrolls the body a wearer can see —
 * exactly one container still captures per page.
 */
function baseContainers(
  contents: PageContents,
  captureTouch = true,
): TextContainerProperty[] {
  const status = new TextContainerProperty({
    containerID: CONTAINER.status.id,
    containerName: CONTAINER.status.name,
    xPosition: 0,
    yPosition: 0,
    width: SCREEN_W - CLOCK_W,
    height: LINE_H,
    paddingLength: 0,
    borderWidth: 0,
    isEventCapture: 0,
    content: contents.status,
  });

  const caption = new TextContainerProperty({
    containerID: CONTAINER.caption.id,
    containerName: CONTAINER.caption.name,
    xPosition: 0,
    yPosition: LINE_H,
    width: SCREEN_W,
    height: CAPTION_H, // whole lines only — no half-line slot to scroll into
    paddingLength: 0,
    borderWidth: 0,
    isEventCapture: 0, // never the session text — see above
    content: contents.caption,
  });

  const clock = new TextContainerProperty({
    containerID: CONTAINER.clock.id,
    containerName: CONTAINER.clock.name,
    xPosition: SCREEN_W - CLOCK_W,
    yPosition: 0,
    width: CLOCK_W,
    height: LINE_H,
    paddingLength: 0,
    borderWidth: 0,
    isEventCapture: 0, // never the clock either — it visibly bounced
    content: contents.clock,
  });

  const touch = new TextContainerProperty({
    containerID: CONTAINER.touch.id,
    containerName: CONTAINER.touch.name,
    xPosition: 0,
    yPosition: LINE_H,
    width: SCREEN_W,
    height: CAPTION_H, // same geometry as the caption band
    paddingLength: 0,
    borderWidth: 0,
    // The sole event-capture container on every page — except a cue page, which
    // hands capture to its scrolling body so the host scrolls it (XERK-133).
    isEventCapture: captureTouch ? 1 : 0,
    content: " ", // renders nothing — the OS bounce moves invisible content
  });

  return [status, caption, clock, touch];
}

/** The one-shot startup layout. Call `createStartUpPageContainer` with this exactly once. */
export function buildStartupContainer(): CreateStartUpPageContainer {
  return new CreateStartUpPageContainer({
    containerTotalNum: 4,
    textObject: baseContainers({ status: "starting…", caption: "", clock: "" }),
  });
}

/** The regular page (no popup), for `rebuildPageContainer` when the popup closes. */
export function buildMainPage(contents: PageContents): RebuildPageContainer {
  return new RebuildPageContainer({
    containerTotalNum: 4,
    textObject: baseContainers(contents),
  });
}

/** The geometry of a popup strip: same container, different size per popup kind. */
interface PopupBox {
  x: number;
  y: number;
  width: number;
  height: number;
  pad: number;
  border: number;
}

const MENU_BOX: PopupBox = {
  x: MENU_X,
  y: MENU_Y,
  width: MENU_W,
  height: MENU_H,
  pad: MENU_PAD,
  border: MENU_BORDER,
};
/**
 * The cue box sized to the cue it holds (XERK-119): a title row over just the
 * body rows it actually renders — the whole body when it fits, else the full
 * CUE_BODY_LINES-row window the host scrolls (XERK-133). A short cue therefore
 * makes a short box, leaving the transcript rows below it visible instead of
 * masking them behind blank rows. A body long enough to scroll always fills the
 * whole window, so this depends on the cue alone, not on the scroll position.
 */
export function cueBox(cue: CueCard, maxLines = CUE_BODY_LINES): PopupBox {
  const rows = cueRows(cue, maxLines);
  return {
    x: CUE_X,
    y: CUE_Y,
    width: CUE_W,
    height: cueHeight(rows),
    pad: CUE_PAD,
    border: CUE_BORDER,
  };
}

/**
 * How many rows the cue's box renders: the title over the visible body rows —
 * the whole body when it fits, else the `maxLines`-row window (CUE_BODY_LINES
 * for a cue, TRANSLATION_BODY_LINES for the translation box, XERK-176). This
 * drives the box height and `cueBodyBox`'s height, so the box, the scrolling
 * body container, and the masked caption range always match (XERK-119).
 */
export function cueRows(cue: CueCard, maxLines = CUE_BODY_LINES): number {
  return 1 + Math.min(cueBodyLines(cue.body).length, maxLines);
}

/**
 * The caption rows the cue's box covers (XERK-119): sized to the cue, so a short
 * box frees the transcript rows a full box would have masked. The controller
 * passes this range to `occludedCaption`.
 */
export function cueRowRange(cue: CueCard, maxLines = CUE_BODY_LINES): [number, number] {
  return cueRowRangeFor(cueBox(cue, maxLines).height);
}

/**
 * The scrolling body container's geometry (XERK-133): it sits inside the cue
 * box's interior, below the pinned title row. Its height is exactly the visible
 * body rows (the whole body when it fits, else the CUE_BODY_LINES-row window);
 * a longer body's full text overflows that height, so the HOST scrolls it with
 * its own native scroll bar. Width is the box interior — the body text wraps a
 * touch narrower (CUE_TEXT_W) so the host's scroll bar has room down the right.
 */
export function cueBodyBox(
  cue: CueCard,
  maxLines = CUE_BODY_LINES,
): { x: number; y: number; width: number; height: number } {
  const visibleRows = Math.min(cueBodyLines(cue.body).length, maxLines);
  return {
    x: CUE_X + CUE_BORDER + CUE_PAD,
    y: CUE_Y + CUE_BORDER + CUE_PAD + LINE_H, // below the pinned title row
    width: CUE_W - 2 * (CUE_BORDER + CUE_PAD),
    height: visibleRows * LINE_H,
  };
}

/** A bordered popup strip container (the menu box, or a cue's title frame). */
function borderedBox(
  container: { id: number; name: string },
  box: PopupBox,
  content: string,
): TextContainerProperty {
  return new TextContainerProperty({
    containerID: container.id,
    containerName: container.name,
    xPosition: box.x,
    yPosition: box.y,
    width: box.width,
    height: box.height,
    paddingLength: box.pad,
    borderWidth: box.border,
    // Any non-black color renders as the HUD's single lit color.
    borderColor: 0xffffff,
    borderRadius: 10,
    isEventCapture: 0,
    content,
  });
}

/**
 * A popup page: the base containers plus one or more popup containers drawn on
 * top (XERK-81). The controller blanks everything the strip covers (status,
 * clock, caption rows via `occludedCaption`), so nothing shows through the box.
 * `captureTouch` stays true for the menu (the invisible overlay keeps capturing
 * swipes to move the highlight); a cue page passes false and lets its scrolling
 * body container capture instead, so the host scrolls it (XERK-133).
 */
function popupPage(
  contents: PageContents,
  popups: TextContainerProperty[],
  captureTouch: boolean,
): RebuildPageContainer {
  return new RebuildPageContainer({
    containerTotalNum: 4 + popups.length,
    textObject: [...baseContainers(contents, captureTouch), ...popups],
  });
}

/** The page with the double-tap menu popup up (Continue / Exit session). */
export function buildMenuPage(contents: PageContents, selected: MenuChoice): RebuildPageContainer {
  return popupPage(contents, [borderedBox(CONTAINER.menu, MENU_BOX, menuText(selected))], true);
}

/**
 * The page with a cue popup up (XERK-81, XERK-133): a bordered box above the
 * live transcript, private to the wearer, auto-dismissed by the controller after
 * ~10s. Two containers make it up: a pinned title row (the bordered frame, with
 * its live countdown) and a scrolling body container inside it. A body that
 * wraps past CUE_BODY_LINES rows overflows the body container, and the host
 * scrolls it with its own native scroll bar — the body container captures the
 * scroll gesture, so a swipe drives that scroll directly. The full cue always
 * lives on the phone Session/History pages regardless.
 */
export function buildCuePage(
  contents: PageContents,
  cue: CueCard,
  secondsLeft?: number,
  maxLines = CUE_BODY_LINES,
): RebuildPageContainer {
  const frame = borderedBox(CONTAINER.menu, cueBox(cue, maxLines), cueTitleLine(cue, secondsLeft));
  const bb = cueBodyBox(cue, maxLines);
  const body = new TextContainerProperty({
    containerID: CONTAINER.cueBody.id,
    containerName: CONTAINER.cueBody.name,
    xPosition: bb.x,
    yPosition: bb.y,
    width: bb.width,
    height: bb.height,
    paddingLength: 0,
    borderWidth: 0,
    // The sole event-capture container while a cue is up (XERK-133): the OS
    // plays its scroll animation on whatever captures the gesture, so making
    // THIS the captor is what scrolls the visible body under a native scroll bar.
    isEventCapture: 1,
    content: cueBodyText(cue),
  });
  return popupPage(contents, [frame, body], false);
}

/** A private context cue shown on the lens (XERK-81). */
export interface CueCard {
  title: string;
  body: string;
  /**
   * Live-source attribution (XERK-120), e.g. "BBC News". Deliberately NOT
   * rendered on the lens: the on-lens box is a tiny monochrome strip where
   * every row costs caption space, so it carries the cue alone — a documented
   * platform exception. The phone Session/History pages (and web/mobile) show
   * the attribution; it rides the CueCard so they can.
   */
  source?: string;
}

/**
 * Lay a left and a right label out on one popup row, the right one flush to
 * the row's right edge (XERK-110). The lens has no alignment control — a row
 * is one string — so the gap is spelled out in spaces, sized by measuring them
 * against the slack the two labels leave. `left` must already be narrow enough
 * to leave room for `right`, which `cueTitleLine` guarantees by wrapping to the
 * reduced width; the single-space floor is a belt-and-braces guard so the two
 * can never run together even if a font measures unexpectedly.
 */
function rowWithRightEdge(left: string, right: string, width: number): string {
  if (!right) return left;
  const spaceWidth = getTextWidth(" ");
  if (spaceWidth <= 0) return `${left} ${right}`;
  const slack = width - getTextWidth(left) - getTextWidth(right);
  let spaces = Math.max(1, Math.floor(slack / spaceWidth));
  // Kerning can make the assembled row measure a hair wider than its parts
  // summed, so check the row itself and give back spaces until it fits (the
  // one-space floor still wins over fitting).
  while (spaces > 1 && getTextWidth(`${left}${" ".repeat(spaces)}${right}`) > width) spaces--;
  return `${left}${" ".repeat(spaces)}${right}`;
}

/** The cue body wrapped into physical rows at the box's interior width. */
export function cueBodyLines(body: string): string[] {
  return wrapLines(body, CUE_TEXT_W);
}

/**
 * The pinned title row of a cue box (XERK-112): the title, in its own case
 * (XERK-134), wrapped to a single row. `secondsLeft` (XERK-110) paints the
 * countdown to auto-dismissal flush to the right edge — the lens counterpart to
 * the countdown in the top-right corner of the web/mobile cue cards. The title
 * is wrapped to whatever width the countdown leaves, so a long one is trimmed
 * instead of pushing the row over the edge. Omitted (no countdown) the row is
 * the title alone. This is the only part of the box the ticker repaints, so a
 * countdown tick never disturbs the host's scroll of the body (XERK-133).
 */
export function cueTitleLine(cue: CueCard, secondsLeft?: number): string {
  const countdown = secondsLeft == null ? "" : cueCountdownLabel(secondsLeft);
  // Reserve the countdown plus one space of separation out of the title's row.
  const reserved = countdown ? getTextWidth(countdown) + getTextWidth(" ") : 0;
  const titleLine = wrapLines(cue.title, CUE_TEXT_W - reserved)[0] ?? cue.title;
  return rowWithRightEdge(titleLine, countdown, CUE_TEXT_W);
}

/**
 * The song box's title (XERK-184): "ARTIST — SONG NAME", the same "ARTIST — TITLE"
 * form the web/mobile lyric cards use — parity across the three front ends. Built
 * as a plain string here; `cueTitleLine` wraps/trims it to the box's title row
 * (with no countdown — a song box has none, it clears on `song.done`).
 */
export function songTitle(artist: string, title: string): string {
  return `${artist} — ${title}`;
}

/**
 * The cue body as the scrolling body container's content (XERK-133): every
 * wrapped row, joined. A body that fits the box's CUE_BODY_LINES rows renders
 * whole; a longer one overflows the container's fixed height and the host
 * scrolls it with its own native scroll bar. Written once when the cue goes up
 * (never on a countdown tick), so the host's scroll position is left alone.
 */
export function cueBodyText(cue: CueCard): string {
  return cueBodyLines(cue.body).join("\n");
}

/**
 * A streaming body tailed to the box's last CUE_BODY_LINES rows (XERK-172). A
 * cue is written once and read from the top — its overflow is the host's to
 * scroll. The translation box instead accumulates turn after turn, and a full
 * rebuild on every new turn resets the host's native scroll back to the top, so
 * the wearer kept seeing the OLDEST rows. Tailing here makes the box behave like
 * the caption band: it keeps the most recent rows and lets older ones fall off
 * the top, so a new turn always arrives at the BOTTOM with no scroll to reset.
 * A body still short of `maxLines` is returned whole (the box stays short,
 * XERK-119); the char bound upstream keeps the accumulated text from growing
 * without limit. The translation box passes TRANSLATION_BODY_LINES (five rows,
 * XERK-176) — one more than a cue — so a run keeps more of its recent turns.
 */
export function tailCueBody(body: string, maxLines = CUE_BODY_LINES): string {
  const rows = cueBodyLines(body);
  return (rows.length > maxLines ? rows.slice(-maxLines) : rows).join("\n");
}

/** Full in-place text replacement for a text container (offset/length 0 = replace all). */
export async function setText(
  bridge: EvenAppBridge,
  container: { id: number; name: string },
  content: string,
): Promise<boolean> {
  return bridge.textContainerUpgrade(
    new TextContainerUpgrade({
      containerID: container.id,
      containerName: container.name,
      contentOffset: 0,
      contentLength: 0,
      content,
    }),
  );
}

/**
 * The slice of the bridge the writer needs — structural, so tests pass a stub.
 * `setText` above satisfies it via a real `EvenAppBridge`.
 */
export type TextWriteFn = (container: { id: number; name: string }, content: string) => Promise<boolean>;

/**
 * Serialized, coalescing writer for lens text (XERK-82).
 *
 * The Even docs are explicit: bridge calls share one BLE link and MUST be
 * serialized — concurrent render calls "can crash the connection" (which
 * presents as the app closing on itself). Fire-and-forget `void setText(...)`
 * from every render therefore has to go through this: one write in flight at a
 * time, and per container only the LATEST text is kept while waiting (captions
 * update far faster than BLE drains, so intermediate frames are dropped, not
 * queued).
 */
export class LensTextWriter {
  private pending = new Map<number, { container: { id: number; name: string }; content: string }>();
  // Whole-page operations (rebuildPageContainer for the popup) that must ride
  // the same serialized BLE lane as the text writes. Drained FIRST, so a
  // rebuild always lands before the re-asserted per-container texts.
  private ops: Array<() => Promise<unknown>> = [];
  private pumping = false;
  // Last content written (or queued) per container: repeat writes of identical
  // text are dropped before they cost a BLE round-trip — the XERK-85 ticker
  // fires every ~600ms but only changed frames may reach the link.
  private last = new Map<number, string>();

  constructor(private readonly write: TextWriteFn) {}

  /** Queue the latest text for a container; starts the drain if idle. No-op when unchanged. */
  set(container: { id: number; name: string }, content: string): void {
    if (this.last.get(container.id) === content) return;
    this.last.set(container.id, content);
    this.pending.set(container.id, { container, content });
    if (!this.pumping) void this.pump();
  }

  /** Queue an arbitrary bridge operation on the serialized lane (e.g. a page rebuild). */
  run(op: () => Promise<unknown>): void {
    this.ops.push(op);
    if (!this.pumping) void this.pump();
  }

  /** Drop the dedupe cache so the next set() always writes (e.g. after re-foregrounding). */
  invalidate(): void {
    this.last.clear();
  }

  /** Resolves once everything queued so far has been written. */
  async flush(): Promise<void> {
    while (this.pumping || this.pending.size > 0 || this.ops.length > 0) {
      await new Promise((r) => setTimeout(r, 10));
    }
  }

  private async pump(): Promise<void> {
    this.pumping = true;
    try {
      while (this.pending.size > 0 || this.ops.length > 0) {
        const op = this.ops.shift();
        if (op) {
          try {
            await op();
          } catch (err) {
            console.warn("tenir: lens page op failed:", err);
          }
          continue;
        }
        const [id, entry] = this.pending.entries().next().value as [
          number,
          { container: { id: number; name: string }; content: string },
        ];
        this.pending.delete(id);
        try {
          await this.write(entry.container, entry.content);
        } catch (err) {
          console.warn("tenir: lens text write failed:", err);
        }
      }
    } finally {
      this.pumping = false;
    }
  }
}

/** The in-session popup's two choices (XERK-85): Continue is the default, on top. */
export type MenuChoice = "continue" | "exit";

/**
 * The in-session popup's rows, rendered inside the bordered menu box:
 * Continue on top (the default) with Exit session below, the highlighted row
 * marked with "›". Swiping moves the highlight; a single tap confirms it
 * (controller.ts).
 */
export function menuText(selected: MenuChoice): string {
  const row = (choice: MenuChoice, label: string) =>
    `${selected === choice ? "›" : " "} ${label}`;
  return `${row("continue", "Continue")}\n${row("exit", "Exit session")}`;
}

/** The animated activity dots (XERK-85): 1 → 2 → 3 dots, cycling with the ticker. */
export function dots(tick: number): string {
  return ".".repeat((tick % 3) + 1);
}

/** The top-right clock text: 12-hour h:MM AM/PM (at most 82px in the EvenHub font). */
export function clockText(date: Date): string {
  const h24 = date.getHours();
  const h = h24 % 12 || 12; // 0 and 12 both show as 12
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${h}:${mm} ${h24 < 12 ? "AM" : "PM"}`;
}

/**
 * The status line, honest about connectivity (XERK-82): outside a session the
 * lens says it is ready rather than pretending to listen, and a dropped or
 * unreachable server is named rather than hidden behind a "×". While recording
 * with an open socket it reads "listening" with dots that move with `tick`
 * (XERK-85) to signify activity.
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

/**
 * Split text into the physical rows the band renders: explicit newlines are
 * respected, and longer paragraphs are greedy-wrapped at word boundaries
 * (pixel-measured via `@evenrealities/pretext`, hard-breaking words that
 * exceed a full row). The result carries OUR breaks: the rendered rows are
 * exactly these strings, each of which fits `maxWidth`.
 */
export function wrapLines(text: string, maxWidth = SCREEN_W - MEASURE_SAFETY_PX): string[] {
  const rows: string[] = [];
  for (const para of text.split("\n")) {
    let rest = para;
    if (rest === "") {
      rows.push("");
      continue;
    }
    while (rest !== "") {
      if (getTextWidth(rest) <= maxWidth) {
        rows.push(rest);
        break;
      }
      // Binary search the longest prefix that fits the row.
      let lo = 1;
      let hi = rest.length - 1;
      let fit = 1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (getTextWidth(rest.slice(0, mid)) <= maxWidth) {
          fit = mid;
          lo = mid + 1;
        } else {
          hi = mid - 1;
        }
      }
      // Prefer breaking at the last space inside the fit; hard-break one
      // over-long unbroken word.
      const space = rest.lastIndexOf(" ", fit);
      const brk = space > 0 ? space : fit;
      rows.push(rest.slice(0, brk));
      rest = rest.slice(brk).replace(/^ +/, "");
    }
  }
  return rows;
}

/**
 * The caption band as exactly `maxLines` physical rows (XERK-85): the LAST
 * rows of the wrapped transcript, top-padded with empty rows so new text
 * keeps arriving at the BOTTOM of the band. With every row measured to fit
 * and exactly CAPTION_LINES of them, the band never overflows — the host has
 * nothing to scroll, and old text simply falls off the top.
 */
export function fitCaptionRows(
  text: string,
  maxLines = CAPTION_LINES,
  maxWidth = SCREEN_W - MEASURE_SAFETY_PX,
): string[] {
  const wrapped = wrapLines(text, maxWidth);
  const kept = wrapped.length > maxLines ? wrapped.slice(-maxLines) : wrapped;
  return [...Array<string>(maxLines - kept.length).fill(""), ...kept];
}

/** `fitCaptionRows` joined for the caption container (empty text stays empty). */
export function fitCaption(
  text: string,
  maxLines = CAPTION_LINES,
  maxWidth = SCREEN_W - MEASURE_SAFETY_PX,
): string {
  if (!text) return "";
  return fitCaptionRows(text, maxLines, maxWidth).join("\n");
}

/**
 * The caption band while the popup is up (XERK-85): the same fitted rows, but
 * the rows the popup box touches are masked to "" — exactly what an opaque
 * popup would hide. Rows above and below keep flowing, so the conversation
 * visibly continues around the box and nothing renders underneath it. The
 * masked range depends on which popup is up: the menu covers MENU rows, the
 * taller cue box covers CUE rows (XERK-112) — the controller passes the range.
 */
export function occludedCaption(
  text: string,
  firstRow = MENU_ROW_FIRST,
  lastRow = MENU_ROW_LAST,
): string {
  if (!text) return "";
  const rows = fitCaptionRows(text);
  for (let r = firstRow; r <= lastRow; r++) rows[r] = "";
  return rows.join("\n");
}

