import { describe, expect, it } from "vitest";

import {
  buildStartupContainer,
  CONTAINER,
  LensTextWriter,
  LINE_H,
  SCREEN_H,
  SCREEN_W,
  statusLine,
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
});

describe("LensTextWriter (XERK-82: bridge calls must be serialized)", () => {
  it("never overlaps writes — each starts only after the previous resolved", async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    const writes: string[] = [];
    const writer = new LensTextWriter(async (_c, content) => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise((r) => setTimeout(r, 5));
      writes.push(content);
      inFlight -= 1;
      return true;
    });

    writer.set(CONTAINER.status, "a");
    writer.set(CONTAINER.caption, "b");
    await writer.flush();

    expect(maxInFlight).toBe(1);
    expect(writes).toEqual(["a", "b"]);
  });

  it("coalesces per container: only the latest queued text is written", async () => {
    const writes: Array<[number, string]> = [];
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const writer = new LensTextWriter(async (c, content) => {
      writes.push([c.id, content]);
      if (writes.length === 1) await gate; // hold the first write in flight
      return true;
    });

    writer.set(CONTAINER.status, "first");
    // While "first" is in flight, three caption updates land — only the last survives.
    writer.set(CONTAINER.caption, "one");
    writer.set(CONTAINER.caption, "two");
    writer.set(CONTAINER.caption, "three");
    release();
    await writer.flush();

    expect(writes).toEqual([
      [CONTAINER.status.id, "first"],
      [CONTAINER.caption.id, "three"],
    ]);
  });

  it("keeps draining after a failed write", async () => {
    const writes: string[] = [];
    const writer = new LensTextWriter(async (_c, content) => {
      if (content === "boom") throw new Error("BLE hiccup");
      writes.push(content);
      return true;
    });
    writer.set(CONTAINER.status, "boom");
    writer.set(CONTAINER.caption, "still works");
    await writer.flush();
    expect(writes).toEqual(["still works"]);
  });
});

describe("statusLine (XERK-82: the lens must not pretend to be running)", () => {
  it("names an unreachable server instead of showing a bare marker", () => {
    expect(statusLine({ connection: "closed", listening: true, micSource: "g2-microphone" })).toBe(
      "server unreachable — retrying",
    );
  });

  it("shows connecting while the socket is opening", () => {
    expect(statusLine({ connection: "connecting", listening: true, micSource: "g2-microphone" })).toBe(
      "connecting to server…",
    );
  });

  it("shows listening/paused with the mic only when actually connected", () => {
    expect(statusLine({ connection: "open", listening: true, micSource: "g2-microphone" })).toBe(
      "listening · g2 mic",
    );
    expect(statusLine({ connection: "open", listening: false, micSource: "phone-microphone" })).toBe(
      "paused · phone mic",
    );
  });
});
