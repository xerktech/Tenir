/**
 * HUD layout tests — the miniapp counterparts of upstream
 * `even/tests/layout.test.ts`'s pure slices (clock, dots, status line,
 * fit-to-band trimming), re-based on the vendored display-utils G2 measurer.
 */

import { describe, expect, it } from "bun:test";

import { G2_PROFILE, TextMeasurer } from "../vendor/display-utils";
import {
  CAPTION_H,
  CAPTION_LINES,
  CAPTION_WRAP_W,
  CAPTION_Y,
  CLOCK_W,
  ELEMENT_IDS,
  IDLE_PROMPT,
  LINE_H,
  SCREEN_H,
  SCREEN_W,
  clockText,
  dots,
  fitCaption,
  fitCaptionRows,
  hudElements,
  statusLine,
  wrapLines,
} from "./hud";

const measurer = new TextMeasurer(G2_PROFILE);

describe("geometry", () => {
  it("derives the caption band from the 576×288 canvas and 40px lines", () => {
    expect(SCREEN_W).toBe(576);
    expect(SCREEN_H).toBe(288);
    expect(LINE_H).toBe(40);
    expect(CAPTION_Y).toBe(40);
    expect(CAPTION_H).toBe(248);
    expect(CAPTION_LINES).toBe(6); // whole lines only — no half-line slot
  });

  it("keeps the widest clock inside its band", () => {
    expect(measurer.measureText("12:59 PM")).toBeLessThanOrEqual(CLOCK_W);
  });
});

describe("wrapLines", () => {
  it("returns short text as a single row", () => {
    expect(wrapLines("hello world")).toEqual(["hello world"]);
  });

  it("respects explicit newlines", () => {
    expect(wrapLines("a\nb")).toEqual(["a", "b"]);
  });

  it("wraps long text into rows that each fit the band", () => {
    const text =
      "the quick brown fox jumps over the lazy dog and keeps running through the long grass toward the horizon without ever slowing down";
    const rows = wrapLines(text);
    expect(rows.length).toBeGreaterThan(1);
    for (const row of rows) {
      expect(measurer.measureText(row)).toBeLessThanOrEqual(CAPTION_WRAP_W);
    }
    // Nothing is lost: every word survives the wrap.
    expect(rows.join(" ").replace(/\s+/g, " ")).toContain("horizon");
  });
});

describe("fitCaption", () => {
  it("returns empty for empty text", () => {
    expect(fitCaption("")).toBe("");
  });

  it("top-pads short text so new rows arrive at the BOTTOM of the band", () => {
    const rows = fitCaptionRows("hello");
    expect(rows).toHaveLength(CAPTION_LINES);
    expect(rows.slice(0, CAPTION_LINES - 1)).toEqual(Array(CAPTION_LINES - 1).fill(""));
    expect(rows[CAPTION_LINES - 1]).toBe("hello");
  });

  it("keeps only the LAST rows when the transcript overflows the band", () => {
    const text = Array.from({ length: 20 }, (_, i) => `line number ${i}`).join("\n");
    const rows = fitCaptionRows(text);
    expect(rows).toHaveLength(CAPTION_LINES);
    expect(rows[CAPTION_LINES - 1]).toBe("line number 19");
    expect(rows[0]).toBe(`line number ${20 - CAPTION_LINES}`);
  });

  it("never exceeds the band's line count", () => {
    const long = "word ".repeat(500);
    expect(fitCaption(long).split("\n")).toHaveLength(CAPTION_LINES);
  });
});

describe("clockText", () => {
  it("renders 12-hour h:MM AM/PM", () => {
    expect(clockText(new Date(2026, 0, 1, 0, 5))).toBe("12:05 AM");
    expect(clockText(new Date(2026, 0, 1, 9, 30))).toBe("9:30 AM");
    expect(clockText(new Date(2026, 0, 1, 12, 0))).toBe("12:00 PM");
    expect(clockText(new Date(2026, 0, 1, 23, 59))).toBe("11:59 PM");
  });
});

describe("dots", () => {
  it("cycles 1 → 2 → 3 dots", () => {
    expect(dots(0)).toBe(".");
    expect(dots(1)).toBe("..");
    expect(dots(2)).toBe("...");
    expect(dots(3)).toBe(".");
  });
});

describe("statusLine", () => {
  it("is honest about connectivity", () => {
    expect(statusLine({ recording: false, connection: "closed" })).toBe("ready");
    expect(statusLine({ recording: true, connection: "connecting" })).toBe(
      "connecting to server…",
    );
    expect(statusLine({ recording: true, connection: "closed" })).toBe(
      "server unreachable — retrying",
    );
    expect(statusLine({ recording: true, connection: "open" }, 1)).toBe("listening..");
  });
});

describe("hudElements", () => {
  it("lays out three stable-id text elements inside the canvas", () => {
    const els = hudElements({ status: "ready", clock: "9:30 AM", caption: IDLE_PROMPT });
    expect(els.map((e) => e.id)).toEqual([
      ELEMENT_IDS.status,
      ELEMENT_IDS.clock,
      ELEMENT_IDS.caption,
    ]);
    for (const e of els) {
      expect(e.type).toBe("text");
      expect(e.box.x).toBeGreaterThanOrEqual(0);
      expect(e.box.y).toBeGreaterThanOrEqual(0);
      expect(e.box.x + e.box.w).toBeLessThanOrEqual(SCREEN_W);
      expect(e.box.y + e.box.h).toBeLessThanOrEqual(SCREEN_H);
    }
    // The status line and clock share the top row without overlapping.
    const [status, clock, caption] = els;
    expect(status.box.x + status.box.w).toBeLessThanOrEqual(clock.box.x);
    expect(caption.box.y).toBe(CAPTION_Y);
  });
});
