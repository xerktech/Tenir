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
