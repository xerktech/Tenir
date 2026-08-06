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
  CUE_ROWS,
  CUE_TEXT_W,
  ELEMENT_IDS,
  IDLE_PROMPT,
  LINE_H,
  MENU_H,
  SCREEN_H,
  SCREEN_W,
  SONG_BODY_LINES,
  TRANSLATION_ROWS,
  cardPopup,
  clockText,
  cueHeight,
  cueRowRangeFor,
  cueTitleLine,
  dots,
  fitCaption,
  fitCaptionRows,
  hudElements,
  menuPopup,
  menuText,
  occludedCaption,
  songBody,
  songTitle,
  statusLine,
  tailCueBody,
  wrapLines,
} from "./hud";

const measurer = new TextMeasurer(G2_PROFILE);

describe("geometry", () => {
  it("derives the caption band from the 576×288 canvas and 40px lines", () => {
    expect(SCREEN_W).toBe(576);
    expect(SCREEN_H).toBe(288);
    expect(LINE_H).toBe(40);
    expect(CAPTION_Y).toBe(40);
    // EXACTLY whole lines (upstream layout.ts): a taller band leaves a
    // half-line slot the host would grow a scroll bar into.
    expect(CAPTION_LINES).toBe(6);
    expect(CAPTION_H).toBe(CAPTION_LINES * LINE_H);
  });

  it("sizes the popup boxes to whole caption-row boundaries", () => {
    // The 2-row menu ends inside the 3rd 40px line (status + two caption
    // rows), the same whole-row rule upstream sized its menu strip to.
    expect(MENU_H).toBeLessThanOrEqual(3 * LINE_H);
    expect(cueRowRangeFor(MENU_H)).toEqual([0, 1]);
    // A full 5-row cue box masks five caption rows, leaving the last flowing.
    expect(cueHeight(CUE_ROWS)).toBeLessThanOrEqual(SCREEN_H);
    expect(cueRowRangeFor(cueHeight(CUE_ROWS))).toEqual([0, 4]);
    // The 6-row translation box still fits the screen.
    expect(cueHeight(TRANSLATION_ROWS)).toBeLessThanOrEqual(SCREEN_H);
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

// ---- popup layer (upstream even/tests/layout.test.ts slices) ----------------

describe("menuText", () => {
  it("marks the highlighted row with ›", () => {
    expect(menuText("continue")).toBe("› Continue\n  Exit session");
    expect(menuText("exit")).toBe("  Continue\n› Exit session");
  });
});

describe("cueTitleLine", () => {
  it("paints the countdown flush to the right edge", () => {
    const row = cueTitleLine({ title: "Employer", body: "" }, 7);
    expect(row.startsWith("Employer")).toBe(true);
    expect(row.endsWith("7s")).toBe(true);
    expect(measurer.measureText(row)).toBeLessThanOrEqual(CUE_TEXT_W);
  });

  it("trims a long title instead of pushing the countdown off the row", () => {
    const row = cueTitleLine({ title: "word ".repeat(40), body: "" }, 10);
    expect(row.endsWith("10s")).toBe(true);
    expect(measurer.measureText(row)).toBeLessThanOrEqual(CUE_TEXT_W);
  });

  it("is the bare title without a countdown", () => {
    expect(cueTitleLine({ title: "Song — Artist", body: "" })).toBe("Song — Artist");
  });
});

describe("tailCueBody", () => {
  it("returns a short body whole", () => {
    expect(tailCueBody("one\ntwo")).toBe("one\ntwo");
  });

  it("keeps the LAST rows when the body overflows", () => {
    const body = Array.from({ length: 9 }, (_, i) => `turn ${i}`).join("\n");
    const kept = tailCueBody(body, 5).split("\n");
    expect(kept).toHaveLength(5);
    expect(kept[4]).toBe("turn 8");
    expect(kept[0]).toBe("turn 4");
  });
});

describe("songBody", () => {
  const lines = (texts: string[]) => texts.map((text, i) => ({ atMs: i * 1000, text }));

  it("marks the current line with > and indents the rest", () => {
    const body = songBody({ lines: lines(["one", "two", "three", "four"]), currentIndex: 1 });
    expect(body.split("\n")).toEqual(["  one", "> two", "  three", "  four"]);
  });

  it("shows the opening lines unmarked before the song starts", () => {
    const body = songBody({ lines: lines(["one", "two", "three"]), currentIndex: -1 });
    expect(body.split("\n")).toEqual(["  one", "  two", "  three"]);
  });

  it("renders ♪ for an instrumental gap and ♪ ♪ ♪ for no lyrics", () => {
    expect(songBody({ lines: [], currentIndex: -1 })).toBe("♪ ♪ ♪");
    const body = songBody({ lines: lines(["one", "", "three"]), currentIndex: 1 });
    expect(body.split("\n")[1]).toBe("> ♪");
  });

  it("never exceeds the box and always keeps the current line's rows", () => {
    const wide = "a very long lyric line that certainly wraps across multiple physical rows of the box";
    const body = songBody({ lines: lines(["one", wide, "three", "four"]), currentIndex: 1 });
    const rows = body.split("\n");
    expect(rows.length).toBeLessThanOrEqual(SONG_BODY_LINES);
    expect(rows.some((r) => r.startsWith("> "))).toBe(true);
  });
});

describe("songTitle", () => {
  it("reads TITLE — ARTIST", () => {
    expect(songTitle("Radiohead", "Weird Fishes")).toBe("Weird Fishes — Radiohead");
  });
});

describe("occludedCaption", () => {
  it("masks exactly the covered rows and leaves the rest flowing", () => {
    const text = Array.from({ length: CAPTION_LINES }, (_, i) => `row ${i}`).join("\n");
    const rows = occludedCaption(text, 0, 1).split("\n");
    expect(rows[0]).toBe("");
    expect(rows[1]).toBe("");
    expect(rows[2]).toBe("row 2");
    expect(rows[CAPTION_LINES - 1]).toBe(`row ${CAPTION_LINES - 1}`);
  });
});

describe("hudElements with a popup", () => {
  it("adds the bordered box and its text on top of the base scene", () => {
    const popup = cardPopup({ title: "Employer", body: "Runs the marina." }, { secondsLeft: 9 });
    const els = hudElements({ status: "", clock: "", caption: "", popup });
    expect(els.map((e) => e.id)).toEqual([
      ELEMENT_IDS.status,
      ELEMENT_IDS.clock,
      ELEMENT_IDS.caption,
      ELEMENT_IDS.popupBox,
      ELEMENT_IDS.popupText,
    ]);
    const box = els[3];
    expect(box.type).toBe("rect");
    expect(box.box).toEqual({ x: 0, y: 0, w: SCREEN_W, h: cueHeight(popup.rows) });
    const text = els[4];
    expect(text.type).toBe("text");
    if (text.type === "text") expect(text.text).toBe(popup.text);
  });

  it("shrinks the box to a short body (XERK-119)", () => {
    const short = cardPopup({ title: "T", body: "one row" });
    expect(short.rows).toBe(2);
    const menu = menuPopup("continue");
    expect(menu.rows).toBe(2);
    expect(cueHeight(short.rows)).toBeLessThan(cueHeight(CUE_ROWS));
  });
});
