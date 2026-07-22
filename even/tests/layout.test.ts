import { describe, expect, it } from "vitest";

import {
  buildStartupContainer,
  CONTAINER,
  CUE_H,
  LINE_H,
  rebuildPlain,
  rebuildWithCue,
  SCREEN_H,
  SCREEN_W,
} from "../src/lens/layout";

describe("lens layout", () => {
  it("declares exactly two containers: status line + caption band", () => {
    const layout = buildStartupContainer();
    expect(layout.containerTotalNum).toBe(2);
    expect(layout.textObject?.map((t) => t.containerName)).toEqual([
      CONTAINER.status.name,
      CONTAINER.caption.name,
    ]);
  });

  it("stacks the status line above a caption band filling the rest of the HUD", () => {
    const [status, caption] = buildStartupContainer().textObject!;
    expect(status.yPosition).toBe(0);
    expect(status.height).toBe(LINE_H);
    expect(caption.yPosition).toBe(LINE_H);
    expect(caption.height).toBe(SCREEN_H - LINE_H);
    expect(status.width).toBe(SCREEN_W);
    expect(caption.width).toBe(SCREEN_W);
  });

  it("marks the caption band as the single event-capture container", () => {
    const [status, caption] = buildStartupContainer().textObject!;
    expect(status.isEventCapture).toBe(0);
    expect(caption.isEventCapture).toBe(1);
  });

  it("rebuilds to the plain two-container layout (no cue box)", () => {
    const layout = rebuildPlain();
    expect(layout.containerTotalNum).toBe(2);
    expect(layout.textObject?.map((t) => t.containerName)).toEqual([
      CONTAINER.status.name,
      CONTAINER.caption.name,
    ]);
    const [, caption] = layout.textObject!;
    expect(caption.yPosition).toBe(LINE_H); // caption reclaims the full band
    expect(caption.height).toBe(SCREEN_H - LINE_H);
    expect(caption.isEventCapture).toBe(1);
  });

  it("rebuilds with a bordered cue box between the status line and a shrunk caption", () => {
    const layout = rebuildWithCue();
    expect(layout.containerTotalNum).toBe(3);
    const [status, cue, caption] = layout.textObject!;
    expect([status.containerName, cue.containerName, caption.containerName]).toEqual([
      CONTAINER.status.name,
      CONTAINER.cue.name,
      CONTAINER.caption.name,
    ]);
    // The cue box sits directly below the status line and carries a visible border.
    expect(cue.yPosition).toBe(LINE_H);
    expect(cue.height).toBe(CUE_H);
    expect(cue.borderWidth).toBeGreaterThan(0);
    expect(cue.isEventCapture).toBe(0);
    // The caption band is pushed down below the cue box and still owns input.
    expect(caption.yPosition).toBe(LINE_H + CUE_H);
    expect(caption.height).toBe(SCREEN_H - LINE_H - CUE_H);
    expect(caption.isEventCapture).toBe(1);
  });
});
