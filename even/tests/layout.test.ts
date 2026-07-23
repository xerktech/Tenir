import { measureTextWrap } from "@evenrealities/pretext";
import { describe, expect, it } from "vitest";

import {
  buildStartupContainer,
  CAPTION_LINES,
  CLOCK_W,
  clockText,
  CONTAINER,
  dots,
  fitCaption,
  LensTextWriter,
  LINE_H,
  SCREEN_H,
  SCREEN_W,
  statusLine,
} from "../src/lens/layout";

describe("lens layout", () => {
  it("declares exactly three containers: status line + caption band + clock", () => {
    const layout = buildStartupContainer();
    expect(layout.containerTotalNum).toBe(3);
    expect(layout.textObject?.map((t) => t.containerName)).toEqual([
      CONTAINER.status.name,
      CONTAINER.caption.name,
      CONTAINER.clock.name,
    ]);
  });

  it("stacks the status line above a caption band filling the rest of the HUD", () => {
    const [status, caption] = buildStartupContainer().textObject!;
    expect(status.yPosition).toBe(0);
    expect(status.height).toBe(LINE_H);
    expect(caption.yPosition).toBe(LINE_H);
    expect(caption.height).toBe(SCREEN_H - LINE_H);
    expect(status.width).toBe(SCREEN_W - CLOCK_W);
    expect(caption.width).toBe(SCREEN_W);
  });

  it("puts the clock in the top-right corner beside the status line (XERK-85)", () => {
    const [, , clock] = buildStartupContainer().textObject!;
    expect(clock.xPosition).toBe(SCREEN_W - CLOCK_W);
    expect(clock.yPosition).toBe(0);
    expect(clock.width).toBe(CLOCK_W);
    expect(clock.height).toBe(LINE_H);
    expect(clock.content).toBe(""); // empty until a session records
  });

  it("marks the caption band as the single event-capture container", () => {
    const [status, caption, clock] = buildStartupContainer().textObject!;
    expect(status.isEventCapture).toBe(0);
    expect(caption.isEventCapture).toBe(1);
    expect(clock.isEventCapture).toBe(0);
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

  it("drops repeat writes of unchanged text (XERK-85: the ticker must not spam BLE)", async () => {
    const writes: string[] = [];
    const writer = new LensTextWriter(async (_c, content) => {
      writes.push(content);
      return true;
    });

    writer.set(CONTAINER.status, "listening.");
    await writer.flush();
    writer.set(CONTAINER.status, "listening."); // unchanged — dropped
    await writer.flush();
    writer.set(CONTAINER.status, "listening.."); // changed — written
    await writer.flush();

    expect(writes).toEqual(["listening.", "listening.."]);
  });

  it("invalidate() forces the next identical write through (repaint after re-foreground)", async () => {
    const writes: string[] = [];
    const writer = new LensTextWriter(async (_c, content) => {
      writes.push(content);
      return true;
    });

    writer.set(CONTAINER.caption, "same");
    await writer.flush();
    writer.invalidate();
    writer.set(CONTAINER.caption, "same");
    await writer.flush();

    expect(writes).toEqual(["same", "same"]);
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
  it("says ready — not listening — when no session is recording", () => {
    expect(statusLine({ recording: false, connection: "closed" })).toBe("ready");
    expect(statusLine({ recording: false, connection: "open" })).toBe("ready");
  });

  it("names an unreachable server instead of showing a bare marker", () => {
    expect(statusLine({ recording: true, connection: "closed" })).toBe(
      "server unreachable — retrying",
    );
  });

  it("shows connecting while the socket is opening", () => {
    expect(statusLine({ recording: true, connection: "connecting" })).toBe(
      "connecting to server…",
    );
  });

  it("shows listening with moving dots while recording on an open socket (XERK-85)", () => {
    expect(statusLine({ recording: true, connection: "open" }, 0)).toBe("listening.");
    expect(statusLine({ recording: true, connection: "open" }, 1)).toBe("listening..");
    expect(statusLine({ recording: true, connection: "open" }, 2)).toBe("listening...");
    expect(statusLine({ recording: true, connection: "open" }, 3)).toBe("listening.");
  });
});

describe("dots (XERK-85: the three dots move to signify activity)", () => {
  it("cycles 1 → 2 → 3 dots and wraps", () => {
    expect([0, 1, 2, 3, 4, 5].map(dots)).toEqual([".", "..", "...", ".", "..", "..."]);
  });
});

describe("clockText", () => {
  it("formats 24h HH:MM, zero-padded", () => {
    expect(clockText(new Date(2026, 6, 22, 9, 5))).toBe("09:05");
    expect(clockText(new Date(2026, 6, 22, 23, 59))).toBe("23:59");
    expect(clockText(new Date(2026, 6, 22, 0, 0))).toBe("00:00");
  });
});

describe("fitCaption (XERK-85: no scrolling — old text falls off the top)", () => {
  it("passes empty text through", () => {
    expect(fitCaption("")).toBe("");
  });

  it("bottom-anchors short text so new text starts at the bottom of the band", () => {
    const fitted = fitCaption("hello");
    expect(fitted).toBe("\n".repeat(CAPTION_LINES - 1) + "hello");
    expect(measureTextWrap(fitted, SCREEN_W).lineCount).toBe(CAPTION_LINES);
  });

  it("trims overflowing text to exactly the lines that fit, keeping the newest", () => {
    const text = Array.from({ length: 30 }, (_, i) => `line ${i}`).join("\n");
    const fitted = fitCaption(text);
    expect(measureTextWrap(fitted, SCREEN_W).lineCount).toBe(CAPTION_LINES);
    expect(fitted.endsWith("line 29")).toBe(true); // newest text survives
    expect(fitted).not.toContain("line 0"); // oldest is gone
  });

  it("accounts for pixel wrapping, not just newlines", () => {
    // One long unbroken paragraph wraps to many lines; the fitted tail must
    // still fit the band exactly and end with the newest words.
    const words = Array.from({ length: 200 }, (_, i) => `word${i}`).join(" ");
    const fitted = fitCaption(words);
    expect(measureTextWrap(fitted, SCREEN_W).lineCount).toBeLessThanOrEqual(CAPTION_LINES);
    expect(fitted.endsWith("word199")).toBe(true);
  });

  it("caps the band height so the host never has overflow to scroll", () => {
    const text = Array.from({ length: 100 }, (_, i) => `segment ${i}`).join("\n");
    const { height } = measureTextWrap(fitCaption(text), SCREEN_W);
    expect(height).toBeLessThanOrEqual(SCREEN_H - LINE_H);
  });
});
