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
  touch: { id: 5, name: "touch" }, // invisible full-band gesture-capture overlay
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
export const MENU_ROW_FIRST = Math.max(0, Math.floor((MENU_Y - LINE_H) / LINE_H));
export const MENU_ROW_LAST = Math.ceil((MENU_Y + MENU_H - LINE_H) / LINE_H) - 1;

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
 * The touch overlay is the ONLY event-capture container on every page
 * (XERK-85): the OS plays its scroll animation on whatever container captures
 * a scroll gesture, so the captured one must render nothing anybody can see.
 * The overlay shares the caption band's exact geometry (the capture target
 * every device-validated build used) but its content is a single space —
 * gestures land on it, and the bounce animation moves invisible content.
 */
function baseContainers(contents: PageContents): TextContainerProperty[] {
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
    isEventCapture: 1, // the single event-capture container, on every page
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

/**
 * The page with the double-tap popup up: the base containers plus the
 * bordered menu strip across the top two lines of the screen. The controller
 * blanks everything the strip covers (status, clock, caption row 0 via
 * `occludedCaption`), so nothing shows through the box.
 */
export function buildMenuPage(contents: PageContents, selected: MenuChoice): RebuildPageContainer {
  const menu = new TextContainerProperty({
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
    isEventCapture: 0,
    content: menuText(selected),
  });
  return new RebuildPageContainer({
    containerTotalNum: 5,
    textObject: [...baseContainers(contents), menu],
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
 * visibly continues around the box and nothing renders underneath it.
 */
export function occludedCaption(text: string): string {
  if (!text) return "";
  const rows = fitCaptionRows(text);
  for (let r = MENU_ROW_FIRST; r <= MENU_ROW_LAST; r++) rows[r] = "";
  return rows.join("\n");
}

