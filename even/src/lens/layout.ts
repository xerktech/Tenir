/**
 * Lens layout for the 576x288 G2 HUD.
 *
 *   ┌──────────────────────┬────────┐  y=0
 *   │ status line (tiny)   │12:59 PM│  h=27  (1 line; clock only in a session)
 *   ├──────────────────────┴────────┤  y=27
 *   │ caption band  (live           │  h=243 (exactly CAPTION_LINES lines —
 *   │ transcript)                   │  ← isEventCapture: 1   no half-line slot)
 *   └───────────────────────────────┘  y=270 (the last 18px stay unused)
 *
 * Exactly ONE container captures input (the caption band). Live text updates go
 * through `textContainerUpgrade` (flicker-free), never a rebuild.
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
  TextContainerProperty,
  TextContainerUpgrade,
} from "@evenrealities/even_hub_sdk";
import { measureTextWrap } from "@evenrealities/pretext";

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
} as const;

/** The one-shot startup layout. Call `createStartUpPageContainer` with this exactly once. */
export function buildStartupContainer(): CreateStartUpPageContainer {
  // paddingLength/borderWidth are pinned to 0 on every container so the width
  // the host wraps at IS the width fitCaption measures at — an unnoticed host
  // default padding would wrap earlier than measured and overflow the band.
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
    content: "starting…",
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
    isEventCapture: 1, // the single event-capture container
    content: "",
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
    isEventCapture: 0,
    content: "",
  });

  return new CreateStartUpPageContainer({
    containerTotalNum: 3,
    textObject: [status, caption, clock],
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

  /** Drop the dedupe cache so the next set() always writes (e.g. after re-foregrounding). */
  invalidate(): void {
    this.last.clear();
  }

  /** Resolves once everything queued so far has been written. */
  async flush(): Promise<void> {
    while (this.pumping || this.pending.size > 0) {
      await new Promise((r) => setTimeout(r, 10));
    }
  }

  private async pump(): Promise<void> {
    this.pumping = true;
    try {
      while (this.pending.size > 0) {
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
 * The in-session popup, rendered in the caption band: Continue on top (the
 * default) with Exit session below, the highlighted row marked with "›".
 * Swiping moves the highlight; a single tap confirms it (controller.ts).
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
