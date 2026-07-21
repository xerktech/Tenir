import { describe, expect, it } from "vitest";

import { buildStartupContainer, CONTAINER, LINE_H, SCREEN_H, SCREEN_W } from "../src/lens/layout";

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
});
