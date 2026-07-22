/**
 * Lens layout for the 576x288 G2 HUD.
 *
 *   ┌───────────────────────────────┐  y=0
 *   │ status line (tiny)            │  h=27  (1 line)
 *   ├───────────────────────────────┤  y=27
 *   │ ⎡ cue box (bordered) ⎤        │  h=66  ← only present while a cue shows
 *   ├───────────────────────────────┤  y=93
 *   │ caption band  (live           │  h=195 (261 with no cue)
 *   │ transcript)                   │  ← isEventCapture: 1
 *   └───────────────────────────────┘  y=288
 *
 * Exactly ONE container captures input (the caption band). Live text updates go
 * through `textContainerUpgrade` (flicker-free). The cue box (XERK-81) is a private
 * context card shown above the transcript for ~10s; because a border can only be set
 * at (re)build time, showing/hiding it uses `rebuildPageContainer` — after which the
 * caller must re-`setText` each container (rebuild clears content).
 */

import {
  CreateStartUpPageContainer,
  type EvenAppBridge,
  RebuildPageContainer,
  TextContainerProperty,
  TextContainerUpgrade,
} from "@evenrealities/even_hub_sdk";

export const SCREEN_W = 576;
export const SCREEN_H = 288;
export const LINE_H = 27; // baked-in LVGL line height
export const CUE_H = 66; // cue box height (title + a wrapped line, with border/padding)
const CUE_BORDER_COLOR = 0xffffff;

export const CONTAINER = {
  status: { id: 1, name: "status" },
  caption: { id: 2, name: "caption" },
  cue: { id: 3, name: "cue" },
} as const;

function statusProp(): TextContainerProperty {
  return new TextContainerProperty({
    containerID: CONTAINER.status.id,
    containerName: CONTAINER.status.name,
    xPosition: 0,
    yPosition: 0,
    width: SCREEN_W,
    height: LINE_H,
    isEventCapture: 0,
    content: "",
  });
}

/** The caption band, sized to whatever vertical space is left below `yPosition`. */
function captionProp(yPosition: number): TextContainerProperty {
  return new TextContainerProperty({
    containerID: CONTAINER.caption.id,
    containerName: CONTAINER.caption.name,
    xPosition: 0,
    yPosition,
    width: SCREEN_W,
    height: SCREEN_H - yPosition,
    isEventCapture: 1, // the single event-capture container
    content: "",
  });
}

/** The bordered cue box that sits between the status line and the caption band. */
function cueProp(): TextContainerProperty {
  return new TextContainerProperty({
    containerID: CONTAINER.cue.id,
    containerName: CONTAINER.cue.name,
    xPosition: 0,
    yPosition: LINE_H,
    width: SCREEN_W,
    height: CUE_H,
    isEventCapture: 0,
    borderWidth: 2,
    borderColor: CUE_BORDER_COLOR,
    borderRadius: 8,
    paddingLength: 4,
    content: "",
  });
}

/** The one-shot startup layout. Call `createStartUpPageContainer` with this exactly once. */
export function buildStartupContainer(): CreateStartUpPageContainer {
  const status = statusProp();
  status.content = "starting…";
  return new CreateStartUpPageContainer({
    containerTotalNum: 2,
    textObject: [status, captionProp(LINE_H)],
  });
}

/** Rebuild to the plain two-container layout (no cue box; caption takes the full band). */
export function rebuildPlain(): RebuildPageContainer {
  return new RebuildPageContainer({
    containerTotalNum: 2,
    textObject: [statusProp(), captionProp(LINE_H)],
  });
}

/** Rebuild to the three-container layout with the bordered cue box shown. */
export function rebuildWithCue(): RebuildPageContainer {
  return new RebuildPageContainer({
    containerTotalNum: 3,
    textObject: [statusProp(), cueProp(), captionProp(LINE_H + CUE_H)],
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
