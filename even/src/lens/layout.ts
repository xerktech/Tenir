/**
 * Lens layout for the 576x288 G2 HUD.
 *
 *   ┌───────────────────────────────┐  y=0
 *   │ status line (tiny)            │  h=27  (1 line)
 *   ├───────────────────────────────┤  y=27
 *   │ caption band  (live           │  h=261
 *   │ transcript)                   │  ← isEventCapture: 1
 *   └───────────────────────────────┘  y=288
 *
 * Exactly ONE container captures input (the caption band). Live text updates go
 * through `textContainerUpgrade` (flicker-free), never a rebuild.
 */

import {
  CreateStartUpPageContainer,
  type EvenAppBridge,
  TextContainerProperty,
  TextContainerUpgrade,
} from "@evenrealities/even_hub_sdk";

export const SCREEN_W = 576;
export const SCREEN_H = 288;
export const LINE_H = 27; // baked-in LVGL line height

export const CONTAINER = {
  status: { id: 1, name: "status" },
  caption: { id: 2, name: "caption" },
} as const;

/** The one-shot startup layout. Call `createStartUpPageContainer` with this exactly once. */
export function buildStartupContainer(): CreateStartUpPageContainer {
  const status = new TextContainerProperty({
    containerID: CONTAINER.status.id,
    containerName: CONTAINER.status.name,
    xPosition: 0,
    yPosition: 0,
    width: SCREEN_W,
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

  return new CreateStartUpPageContainer({
    containerTotalNum: 2,
    textObject: [status, caption],
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

  constructor(private readonly write: TextWriteFn) {}

  /** Queue the latest text for a container; starts the drain if idle. */
  set(container: { id: number; name: string }, content: string): void {
    this.pending.set(container.id, { container, content });
    if (!this.pumping) void this.pump();
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

/**
 * The status line, honest about connectivity (XERK-82): before sign-in the lens
 * says so instead of pretending to listen, and a dropped/unreachable server is
 * named rather than hidden behind a "×".
 */
export function statusLine(state: {
  connection: "connecting" | "open" | "closed";
  listening: boolean;
  micSource: string;
}): string {
  if (state.connection === "connecting") return "connecting to server…";
  if (state.connection === "closed") return "server unreachable — retrying";
  const mic = state.micSource === "g2-microphone" ? "g2 mic" : "phone mic";
  return `${state.listening ? "listening" : "paused"} · ${mic}`;
}
