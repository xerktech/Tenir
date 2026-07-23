/**
 * Lens layout for the 576x288 G2 HUD.
 *
 *   ┌──────────────────────┬────────┐  y=0
 *   │ status line (tiny)   │12:59 PM│  h=27  (1 line; clock only in a session)
 *   ├──────────────────────┴────────┤  y=27
 *   │ caption band  (live           │  h=243 (exactly CAPTION_LINES lines —
 *   │ transcript)                   │  no half-line slot)
 *   └───────────────────────────────┘  y=270 (the last 18px stay unused)
 *
 * Exactly ONE container captures input per page — and it is NEVER the caption
 * band (XERK-85 feedback: a scroll gesture aimed at the captured container
 * triggers the OS scroll animation on it, which must not happen on the session
 * text). The plain pages give capture to the tiny clock container; the popup
 * page gives it to the popup list, where the OS turns scrolling into moving
 * the selection. Live text updates go through `textContainerUpgrade`
 * (flicker-free), never a rebuild.
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
  ListContainerProperty,
  ListItemContainerProperty,
  RebuildPageContainer,
  TextContainerProperty,
  TextContainerUpgrade,
} from "@evenrealities/even_hub_sdk";
import { getTextWidth, measureTextWrap } from "@evenrealities/pretext";

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
  menu: { id: 4, name: "menu" }, // the double-tap popup box (only on the menu page)
} as const;

// ---- the double-tap popup (XERK-85) -----------------------------------------
// A native OS LIST container overlaid on the page via `rebuildPageContainer`
// (the SDK's sanctioned runtime page change), horizontally centered in the
// upper part of the caption band. The OS renders the menu — its background,
// the selection border (isItemSelectBorderEn) — so the session text does not
// show through, and it owns the gestures while open: scrolling moves the
// SELECTION (never scrolls the session text) and a tap reports the selected
// item back through the listEvent channel.
export const MENU_ITEMS = ["Continue", "Exit session"] as const; // Continue = default, on top
export const MENU_EXIT_INDEX = 1;
export const MENU_PAD = 10;
export const MENU_BORDER = 2;
const MENU_LABEL_MAX_W = Math.max(...MENU_ITEMS.map(getTextWidth));
// Widest label + padding/border each side + slack for the OS list cell chrome
// (kept even so the centered x position is a whole pixel).
export const MENU_W = 2 * Math.ceil((MENU_LABEL_MAX_W + 2 * (MENU_PAD + MENU_BORDER) + 24) / 2);
// Two list cells; each gets headroom over the bare line height for the OS
// cell padding + selection border.
export const MENU_H = 2 * (LINE_H + 10) + 2 * (MENU_PAD + MENU_BORDER);
export const MENU_X = (SCREEN_W - MENU_W) / 2;
export const MENU_Y = 2 * LINE_H; // one caption row below the status line

/** The text every base container carries when a page is (re)built. */
export interface PageContents {
  status: string;
  caption: string;
  clock: string;
}

/**
 * The three always-present containers: status line, caption band, clock.
 * paddingLength/borderWidth are pinned to 0 on each so the width the host
 * wraps at IS the width fitCaption measures at — an unnoticed host default
 * padding would wrap earlier than measured and overflow the band.
 *
 * The caption band NEVER captures input (XERK-85: a scroll gesture on the
 * captured container triggers the OS scroll animation there — the session text
 * must never be its target). The plain pages capture on the tiny clock
 * container instead; the popup page captures on the popup list.
 */
function baseContainers(contents: PageContents, captureOnClock: boolean): TextContainerProperty[] {
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
    isEventCapture: 0, // never — see above
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
    isEventCapture: captureOnClock ? 1 : 0,
    content: contents.clock,
  });

  return [status, caption, clock];
}

/** The one-shot startup layout. Call `createStartUpPageContainer` with this exactly once. */
export function buildStartupContainer(): CreateStartUpPageContainer {
  return new CreateStartUpPageContainer({
    containerTotalNum: 3,
    textObject: baseContainers({ status: "starting…", caption: "", clock: "" }, true),
  });
}

/** The regular page (no popup), for `rebuildPageContainer` when the popup closes. */
export function buildMainPage(contents: PageContents): RebuildPageContainer {
  return new RebuildPageContainer({
    containerTotalNum: 3,
    textObject: baseContainers(contents, true),
  });
}

/**
 * The page with the double-tap popup up: the three base containers plus the
 * native OS list rendering Continue / Exit session. The list is the page's
 * event-capture container, so the OS owns the gestures while the popup is
 * open — scrolling moves the selection (with its own selection border), and a
 * tap reports the chosen item via the listEvent channel.
 */
export function buildMenuPage(contents: PageContents): RebuildPageContainer {
  const menu = new ListContainerProperty({
    containerID: CONTAINER.menu.id,
    containerName: CONTAINER.menu.name,
    xPosition: MENU_X,
    yPosition: MENU_Y,
    width: MENU_W,
    height: MENU_H,
    paddingLength: MENU_PAD,
    borderWidth: MENU_BORDER,
    // Any non-black color renders as the HUD's single lit color.
    borderColor: 0xffffff,
    borderRadius: 10,
    isEventCapture: 1, // the popup owns the gestures while it is up
    itemContainer: new ListItemContainerProperty({
      itemCount: MENU_ITEMS.length,
      itemName: [...MENU_ITEMS],
      itemWidth: MENU_W - 2 * (MENU_PAD + MENU_BORDER),
      isItemSelectBorderEn: 1, // the OS draws the selection highlight
    }),
  });
  // Best-effort black backdrop (XERK-85: the session text must not show
  // through the popup): the SDK's toJson passes unknown keys through to the
  // host, so declare an opaque black background for hosts that honor it.
  (menu as unknown as Record<string, unknown>).backgroundColor = 0x000000;
  return new RebuildPageContainer({
    containerTotalNum: 4,
    textObject: baseContainers(contents, false),
    listObject: [menu],
  });
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
 * Fit the live transcript to the caption band (XERK-85).
 *
 * Trims to the LAST `maxLines` wrapped lines (pixel-accurate via
 * `@evenrealities/pretext`, which mirrors the LVGL renderer) so the band never
 * overflows — with nothing overflowing, the host has nothing to scroll. Old
 * text that no longer fits simply isn't there anymore. The kept tail is
 * top-padded with newlines so new text keeps arriving at the BOTTOM of the
 * band, exactly as it does mid-session when the band is full.
 *
 * Measured at `SCREEN_W - MEASURE_SAFETY_PX` by default: any drift between the
 * measured and the rendered wrap then trims a line too early (invisible)
 * instead of leaving one line too many (clipped half-way + a scroll bar).
 */
export function fitCaption(
  text: string,
  maxLines = CAPTION_LINES,
  maxWidth = SCREEN_W - MEASURE_SAFETY_PX,
): string {
  if (!text) return "";
  const lines = (t: string) => measureTextWrap(t, maxWidth).lineCount;
  let kept = text;
  if (lines(text) > maxLines) {
    // Binary search the smallest suffix that still fits: O(log n) measures.
    let lo = 1; // dropping 0 chars is known not to fit
    let hi = text.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (lines(text.slice(mid)) <= maxLines) hi = mid;
      else lo = mid + 1;
    }
    kept = text.slice(lo);
  }
  return "\n".repeat(maxLines - lines(kept)) + kept;
}

