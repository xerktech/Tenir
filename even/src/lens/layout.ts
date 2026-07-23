/**
 * Lens layout for the 576x288 G2 HUD.
 *
 *   ┌────────────────────────┬──────┐  y=0
 *   │ status line (tiny)     │ HH:MM│  h=27  (1 line; clock only in a session)
 *   ├────────────────────────┴──────┤  y=27
 *   │ caption band  (live           │  h=261
 *   │ transcript)                   │  ← isEventCapture: 1
 *   └───────────────────────────────┘  y=288
 *
 * Exactly ONE container captures input (the caption band). Live text updates go
 * through `textContainerUpgrade` (flicker-free), never a rebuild.
 *
 * XERK-85: while a session is recording the status line reads "listening" with
 * animated dots and the clock container shows the current time top-right. The
 * caption band is trimmed to the tail that FITS the band (`fitCaption`) so the
 * host never has overflow to scroll — old text simply falls off the top.
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

// The clock band, top-right. HH:MM renders 52px in the EvenHub font (digits are
// tabular), so 64px keeps a small breathing margin off the right edge.
export const CLOCK_W = 64;

// How many whole lines fit the caption band. Content is always trimmed to this,
// so the band never overflows — and an overflow-free container has nothing for
// the host to scroll (XERK-85: no scrolling while recording).
export const CAPTION_LINES = Math.floor((SCREEN_H - LINE_H) / LINE_H);

export const CONTAINER = {
  status: { id: 1, name: "status" },
  caption: { id: 2, name: "caption" },
  clock: { id: 3, name: "clock" },
} as const;

/** The one-shot startup layout. Call `createStartUpPageContainer` with this exactly once. */
export function buildStartupContainer(): CreateStartUpPageContainer {
  const status = new TextContainerProperty({
    containerID: CONTAINER.status.id,
    containerName: CONTAINER.status.name,
    xPosition: 0,
    yPosition: 0,
    width: SCREEN_W - CLOCK_W,
    height: LINE_H,
    isEventCapture: 0,
    content: "starting…",
  });

  const caption = new TextContainerProperty({
    containerID: CONTAINER.caption.id,
    containerName: CONTAINER.caption.name,
    xPosition: 0,
    yPosition: LINE_H,
    width: SCREEN_W,
    height: SCREEN_H - LINE_H,
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

/** The animated activity dots (XERK-85): 1 → 2 → 3 dots, cycling with the ticker. */
export function dots(tick: number): string {
  return ".".repeat((tick % 3) + 1);
}

/** The top-right clock text: 24h HH:MM (fixed 52px in the EvenHub font). */
export function clockText(date: Date): string {
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
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
 */
export function fitCaption(text: string, maxLines = CAPTION_LINES, maxWidth = SCREEN_W): string {
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
