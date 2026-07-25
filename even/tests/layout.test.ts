import { getTextWidth, measureTextWrap } from "@evenrealities/pretext";
import { describe, expect, it } from "vitest";

import {
  buildCuePage,
  buildMainPage,
  buildMenuPage,
  buildStartupContainer,
  cueBodyLines,
  cueBox,
  cueDetail,
  cueHeight,
  cueMaxScroll,
  cueRowRange,
  cueRows,
  cueText,
  CAPTION_H,
  CAPTION_LINES,
  CLOCK_W,
  clockText,
  CONTAINER,
  CUE_BODY_LINES,
  CUE_H,
  CUE_ROW_FIRST,
  CUE_ROW_LAST,
  CUE_ROWS,
  CUE_TEXT_W,
  dots,
  fitCaption,
  LensTextWriter,
  fitCaptionRows,
  LINE_H,
  MEASURE_SAFETY_PX,
  MENU_BORDER,
  MENU_H,
  MENU_PAD,
  MENU_ROW_FIRST,
  MENU_ROW_LAST,
  MENU_W,
  MENU_Y,
  menuText,
  occludedCaption,
  SCREEN_W,
  statusLine,
  wrapLines,
} from "../src/lens/layout";

// The width fitCaption measures wrapping at (see MEASURE_SAFETY_PX).
const FIT_W = SCREEN_W - MEASURE_SAFETY_PX;

describe("lens layout", () => {
  it("declares four containers: status + caption band + clock + touch overlay", () => {
    const layout = buildStartupContainer();
    expect(layout.containerTotalNum).toBe(4);
    expect(layout.textObject?.map((t) => t.containerName)).toEqual([
      CONTAINER.status.name,
      CONTAINER.caption.name,
      CONTAINER.clock.name,
      CONTAINER.touch.name,
    ]);
  });

  it("stacks the status line above a whole-lines-only caption band", () => {
    const [status, caption] = buildStartupContainer().textObject!;
    expect(status.yPosition).toBe(0);
    expect(status.height).toBe(LINE_H);
    expect(caption.yPosition).toBe(LINE_H);
    expect(caption.height).toBe(CAPTION_H);
    // An exact multiple of the line height: a half-line slot at the bottom
    // would show a clipped line and grow a scroll bar to reach the rest.
    expect(CAPTION_H % LINE_H).toBe(0);
    expect(CAPTION_H).toBe(CAPTION_LINES * LINE_H);
    expect(status.width).toBe(SCREEN_W - CLOCK_W);
    expect(caption.width).toBe(SCREEN_W);
  });

  it("pins padding and border to 0 so the host wraps at the measured width", () => {
    for (const c of buildStartupContainer().textObject!) {
      expect(c.paddingLength).toBe(0);
      expect(c.borderWidth).toBe(0);
    }
  });

  it("puts the clock in the top-right corner beside the status line (XERK-85)", () => {
    const [, , clock] = buildStartupContainer().textObject!;
    expect(clock.xPosition).toBe(SCREEN_W - CLOCK_W);
    expect(clock.yPosition).toBe(0);
    expect(clock.width).toBe(CLOCK_W);
    expect(clock.height).toBe(LINE_H);
    expect(clock.content).toBe(""); // empty until signed in
  });

  it("captures input ONLY on the invisible touch overlay (XERK-85)", () => {
    const [status, caption, clock, touch] = buildStartupContainer().textObject!;
    // A scroll gesture animates the captured container: never the session
    // text, never the clock (both visibly bounced on device).
    expect(status.isEventCapture).toBe(0);
    expect(caption.isEventCapture).toBe(0);
    expect(clock.isEventCapture).toBe(0);
    expect(touch.isEventCapture).toBe(1);
  });

  it("the touch overlay shares the caption band's geometry but renders nothing", () => {
    const [, caption, , touch] = buildStartupContainer().textObject!;
    expect(touch.xPosition).toBe(caption.xPosition);
    expect(touch.yPosition).toBe(caption.yPosition);
    expect(touch.width).toBe(caption.width);
    expect(touch.height).toBe(caption.height);
    expect(touch.content).toBe(" "); // a single space — invisible when bounced
    expect(touch.borderWidth).toBe(0);
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

  it("run() ops ride the serialized lane; stale pending writes never land after them", async () => {
    const events: string[] = [];
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const writer = new LensTextWriter(async (_c, content) => {
      events.push(`set:${content}`);
      if (events.length === 1) await gate; // hold the first write in flight
      return true;
    });

    writer.set(CONTAINER.status, "first"); // in flight
    writer.set(CONTAINER.caption, "stale"); // queued before the rebuild
    writer.run(async () => {
      events.push("rebuild");
    });
    writer.invalidate();
    writer.set(CONTAINER.caption, "fresh"); // re-asserted content coalesces over "stale"
    release();
    await writer.flush();

    // The rebuild runs before the texts, and the pre-rebuild caption content
    // never lands on the rebuilt page.
    expect(events).toEqual(["set:first", "rebuild", "set:fresh"]);
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
  it("formats 12-hour h:MM AM/PM", () => {
    expect(clockText(new Date(2026, 6, 22, 9, 5))).toBe("9:05 AM");
    expect(clockText(new Date(2026, 6, 22, 23, 59))).toBe("11:59 PM");
    expect(clockText(new Date(2026, 6, 22, 12, 30))).toBe("12:30 PM");
    expect(clockText(new Date(2026, 6, 22, 0, 0))).toBe("12:00 AM");
  });
});

describe("fitCaption (XERK-85: no scrolling — old text falls off the top)", () => {
  it("passes empty text through", () => {
    expect(fitCaption("")).toBe("");
  });

  it("bottom-anchors short text so new text starts at the bottom of the band", () => {
    const fitted = fitCaption("hello");
    expect(fitted).toBe("\n".repeat(CAPTION_LINES - 1) + "hello");
    expect(measureTextWrap(fitted, FIT_W).lineCount).toBe(CAPTION_LINES);
  });

  it("trims overflowing text to exactly the lines that fit, keeping the newest", () => {
    const text = Array.from({ length: 30 }, (_, i) => `line ${i}`).join("\n");
    const fitted = fitCaption(text);
    expect(measureTextWrap(fitted, FIT_W).lineCount).toBe(CAPTION_LINES);
    expect(fitted.endsWith("line 29")).toBe(true); // newest text survives
    expect(fitted).not.toContain("line 0"); // oldest is gone
  });

  it("accounts for pixel wrapping, not just newlines", () => {
    // One long unbroken paragraph wraps to many lines; the fitted tail must
    // still fit the band exactly and end with the newest words.
    const words = Array.from({ length: 200 }, (_, i) => `word${i}`).join(" ");
    const fitted = fitCaption(words);
    expect(measureTextWrap(fitted, FIT_W).lineCount).toBe(CAPTION_LINES);
    expect(fitted.endsWith("word199")).toBe(true);
  });

  it("measures a touch narrow so wrap drift can only trim early, never overflow", () => {
    // Even measured at the FULL band width, the fitted text must not exceed
    // the band: the safety margin absorbs measure-vs-render drift.
    const words = Array.from({ length: 200 }, (_, i) => `word${i}`).join(" ");
    const { height, lineCount } = measureTextWrap(fitCaption(words), SCREEN_W);
    expect(lineCount).toBeLessThanOrEqual(CAPTION_LINES);
    expect(height).toBeLessThanOrEqual(CAPTION_H);
  });

  it("caps the band height so the host never has overflow to scroll", () => {
    const text = Array.from({ length: 100 }, (_, i) => `segment ${i}`).join("\n");
    const { height } = measureTextWrap(fitCaption(text), FIT_W);
    expect(height).toBeLessThanOrEqual(CAPTION_H);
  });
});

describe("wrapLines", () => {
  it("respects explicit newlines", () => {
    expect(wrapLines("a\nb\n\nc")).toEqual(["a", "b", "", "c"]);
  });

  it("wraps long paragraphs at word boundaries into rows that all fit", () => {
    const words = Array.from({ length: 60 }, (_, i) => `word${i}`).join(" ");
    const rows = wrapLines(words);
    expect(rows.length).toBeGreaterThan(1);
    for (const row of rows) expect(getTextWidth(row)).toBeLessThanOrEqual(FIT_W);
    // Nothing lost or reordered: the rows re-join into the original words.
    expect(rows.join(" ")).toBe(words);
  });

  it("hard-breaks a single word wider than a row", () => {
    const monster = "x".repeat(400);
    const rows = wrapLines(monster);
    expect(rows.length).toBeGreaterThan(1);
    for (const row of rows) expect(getTextWidth(row)).toBeLessThanOrEqual(FIT_W);
    expect(rows.join("")).toBe(monster);
  });
});

describe("menuText (XERK-85: the double-tap popup)", () => {
  it("puts Continue on top as the default, Exit session below", () => {
    expect(menuText("continue")).toBe("› Continue\n  Exit session");
  });

  it("moves the highlight to Exit session", () => {
    expect(menuText("exit")).toBe("  Continue\n› Exit session");
  });
});

describe("occludedCaption (XERK-85: nothing shows through the popup box)", () => {
  it("masks exactly the rows the box touches; the rest keep flowing", () => {
    const text = Array.from({ length: 30 }, (_, i) => `line ${i}`).join("\n");
    const plain = fitCaptionRows(text);
    const rows = occludedCaption(text).split("\n");
    expect(rows).toHaveLength(CAPTION_LINES);
    for (let r = 0; r < CAPTION_LINES; r++) {
      if (r >= MENU_ROW_FIRST && r <= MENU_ROW_LAST) expect(rows[r]).toBe("");
      else expect(rows[r]).toBe(plain[r]); // unmasked rows are untouched
    }
    expect(rows[CAPTION_LINES - 1]).toBe("line 29"); // newest text still at the bottom
  });

  it("passes empty text through", () => {
    expect(occludedCaption("")).toBe("");
  });

  it("the masked rows cover the box's whole overlap with the caption band", () => {
    // The strip extends above the band too (status + clock — blanked by the
    // controller, not the caption mask); the mask must cover the band part.
    const maskTop = LINE_H + MENU_ROW_FIRST * LINE_H;
    const maskBottom = LINE_H + (MENU_ROW_LAST + 1) * LINE_H;
    expect(maskTop).toBeLessThanOrEqual(Math.max(MENU_Y, LINE_H));
    expect(maskBottom).toBeGreaterThanOrEqual(MENU_Y + MENU_H);
  });

  it("masks the taller cue box's rows when given its range (XERK-112)", () => {
    const text = Array.from({ length: 30 }, (_, i) => `line ${i}`).join("\n");
    const plain = fitCaptionRows(text);
    const rows = occludedCaption(text, CUE_ROW_FIRST, CUE_ROW_LAST).split("\n");
    // The cue box covers strictly more rows than the menu.
    expect(CUE_ROW_LAST).toBeGreaterThan(MENU_ROW_LAST);
    for (let r = 0; r < CAPTION_LINES; r++) {
      if (r >= CUE_ROW_FIRST && r <= CUE_ROW_LAST) expect(rows[r]).toBe("");
      else expect(rows[r]).toBe(plain[r]);
    }
    // The cue mask must cover the band part of the cue box's overlap.
    const maskBottom = LINE_H + (CUE_ROW_LAST + 1) * LINE_H;
    expect(maskBottom).toBeGreaterThanOrEqual(CUE_H);
  });
});

describe("popup pages (XERK-85: a bordered box over the live conversation)", () => {
  const CONTENTS = { status: "s", caption: "c", clock: "t" };
  const menuOf = (page: ReturnType<typeof buildMenuPage>) => {
    const containers = page.textObject!;
    return containers[containers.length - 1]!;
  };

  it("main page: the four base containers carrying their contents", () => {
    const page = buildMainPage(CONTENTS);
    expect(page.containerTotalNum).toBe(4);
    expect(page.textObject!.map((c) => c.content)).toEqual(["s", "c", "t", " "]);
  });

  it("menu page adds the bordered box LAST so it draws on top", () => {
    const page = buildMenuPage(CONTENTS, "continue");
    expect(page.containerTotalNum).toBe(5);
    const menu = menuOf(page);
    expect(menu.containerName).toBe(CONTAINER.menu.name);
    expect(menu.borderWidth).toBe(MENU_BORDER);
    expect(MENU_BORDER).toBeGreaterThan(0); // an actual bordered box
    expect(menu.content).toBe("› Continue\n  Exit session");
    expect(menuOf(buildMenuPage(CONTENTS, "exit")).content).toBe("  Continue\n› Exit session");
  });

  it("keeps capture on the touch overlay even while the popup is up", () => {
    const page = buildMenuPage(CONTENTS, "continue");
    const captures = page.textObject!.filter((c) => c.isEventCapture === 1);
    expect(captures.map((c) => c.containerName)).toEqual([CONTAINER.touch.name]);
  });

  it("spans the full width from the top, padded but never past the third line", () => {
    const menu = menuOf(buildMenuPage(CONTENTS, "continue"));
    expect(menu.xPosition).toBe(0);
    expect(menu.width).toBe(SCREEN_W); // full width
    expect(menu.yPosition).toBe(0); // from the very top
    expect(menu.paddingLength).toBeGreaterThan(0); // breathing room around the rows
    // Two option rows + symmetric padding + border…
    expect(menu.height).toBe(2 * LINE_H + 2 * (MENU_PAD + MENU_BORDER));
    // …but the strip must end inside the third transcript row's boundary, so
    // opening the popup never costs an extra transcript line.
    expect(menu.height!).toBeLessThanOrEqual(3 * LINE_H);
  });

  it("both labels fit the box interior without wrapping", () => {
    const interior = MENU_W - 2 * (MENU_PAD + MENU_BORDER);
    expect(getTextWidth("› Exit session")).toBeLessThanOrEqual(interior);
    expect(getTextWidth("› Continue")).toBeLessThanOrEqual(interior);
  });
});

describe("cue popup (XERK-81)", () => {
  const cue = { title: "Sun", body: "About 150 million km away." };

  it("fits a cue into the upper-cased title over its body (short body: one row)", () => {
    const rows = cueText(cue).split("\n");
    expect(rows).toHaveLength(2); // title + the single body row this body wraps to
    expect(rows[0]).toBe("SUN");
    expect(rows[1].length).toBeGreaterThan(0);
    // Each row fits the popup's interior width (never wider than the box).
    for (const row of rows) expect(getTextWidth(row)).toBeLessThanOrEqual(CUE_TEXT_W);
  });

  it("builds the cue strip on the same container, sized to the cue's rows", () => {
    const page = buildCuePage({ status: "listening", caption: "hi", clock: "2:05 PM" }, cue, 7);
    expect(page.containerTotalNum).toBe(5);
    const popup = page.textObject!.find((t) => t.containerName === CONTAINER.menu.name)!;
    expect(popup.yPosition).toBe(MENU_Y); // a strip from the top of the screen
    // Sized to the cue it holds (XERK-119): this short body is one row, so the
    // box is a title + one body row — never the full CUE_H.
    expect(cueRows(cue)).toBe(2);
    expect(popup.height).toBe(cueHeight(2));
    expect(popup.height!).toBeLessThan(CUE_H);
    expect(popup.borderWidth).toBe(MENU_BORDER);
    expect(popup.isEventCapture).toBe(0);
    expect(popup.content).toBe(cueText(cue, 7));
  });

  it("builds the full-height cue strip for a body that fills the box (XERK-119)", () => {
    const long = {
      title: "History",
      body: Array.from({ length: 60 }, (_, i) => `word${i}`).join(" "),
    };
    const page = buildCuePage({ status: "listening", caption: "hi", clock: "2:05 PM" }, long, 7);
    const popup = page.textObject!.find((t) => t.containerName === CONTAINER.menu.name)!;
    expect(cueRows(long)).toBe(CUE_ROWS); // title + a full CUE_BODY_LINES window
    expect(popup.height).toBe(CUE_H); // taller than the menu — room for 3 body rows
    expect(popup.height!).toBeGreaterThan(MENU_H);
  });

  describe("countdown to dismissal (XERK-110)", () => {
    it("ends the title row with the seconds left, flush to the right edge", () => {
      const rows = cueText(cue, 7).split("\n");
      expect(rows).toHaveLength(2); // title + the one body row this short body wraps to
      expect(rows[0]).toMatch(/^SUN {2,}7s$/); // title left, count right, gap between
      // The count sits as far right as whole spaces reach: one more space would
      // push it past the row.
      const spaceWidth = getTextWidth(" ");
      expect(getTextWidth(rows[0])).toBeLessThanOrEqual(CUE_TEXT_W);
      expect(getTextWidth(rows[0]) + spaceWidth).toBeGreaterThan(CUE_TEXT_W);
    });

    it("keeps every count from 10s down to 0s inside the box", () => {
      for (let s = 10; s >= 0; s--) {
        const rows = cueText(cue, s).split("\n");
        expect(rows).toHaveLength(2);
        expect(rows[0].endsWith(`${s}s`)).toBe(true);
        for (const row of rows) expect(getTextWidth(row)).toBeLessThanOrEqual(CUE_TEXT_W);
      }
    });

    it("trims a long title rather than letting it push the count off the row", () => {
      const long = {
        title: "A ludicrously long cue title that could never fit one lens row",
        body: "Body.",
      };
      const rows = cueText(long, 3).split("\n");
      expect(rows).toHaveLength(2);
      expect(rows[0].endsWith("3s")).toBe(true);
      expect(getTextWidth(rows[0])).toBeLessThanOrEqual(CUE_TEXT_W);
      // The title lost its tail to make room; what remains is its own start.
      expect(long.title.toUpperCase().startsWith(rows[0].split("  ")[0])).toBe(true);
    });

    it("leaves the row as the bare title when no countdown is given", () => {
      expect(cueText(cue)).toBe(`SUN\n${cueText(cue).split("\n")[1]}`);
      expect(cueText(cue).split("\n")[0]).toBe("SUN");
    });
  });

  describe("three body rows, scrollable (XERK-112)", () => {
    // A body long enough to wrap well past the box's three visible rows.
    const long = {
      title: "History",
      body: Array.from({ length: 60 }, (_, i) => `word${i}`).join(" "),
    };

    it("gives the cue box CUE_ROWS rows: a title over CUE_BODY_LINES body rows", () => {
      expect(CUE_ROWS).toBe(1 + CUE_BODY_LINES);
      expect(CUE_BODY_LINES).toBe(3);
      // The box height is exactly CUE_ROWS whole lines plus padding + border.
      expect(CUE_H).toBe(CUE_ROWS * LINE_H + 2 * (MENU_PAD + MENU_BORDER));
      // …and it stays within a whole number of transcript rows (no half-line the
      // host would grow a scroll bar to reach).
      expect(CUE_H).toBeLessThanOrEqual((CUE_ROWS + 1) * LINE_H);
    });

    it("shows the title over the first CUE_BODY_LINES body rows, each fitting the box", () => {
      const rows = cueText(long).split("\n");
      expect(rows).toHaveLength(1 + CUE_BODY_LINES); // title + three body rows
      expect(rows[0]).toBe("HISTORY");
      const body = cueBodyLines(long.body);
      expect(rows.slice(1)).toEqual(body.slice(0, CUE_BODY_LINES));
      for (const row of rows) expect(getTextWidth(row)).toBeLessThanOrEqual(CUE_TEXT_W);
    });

    it("reports how far a long body can scroll, and zero for one that fits", () => {
      const body = cueBodyLines(long.body);
      expect(body.length).toBeGreaterThan(CUE_BODY_LINES);
      expect(cueMaxScroll(long.body)).toBe(body.length - CUE_BODY_LINES);
      expect(cueMaxScroll(cue.body)).toBe(0); // the short body fits — nothing to scroll
    });

    it("windows the body down as it scrolls, never past the last rows", () => {
      const body = cueBodyLines(long.body);
      const max = cueMaxScroll(long.body);
      // Scrolled by one: the window drops a row.
      expect(cueText(long, undefined, 1).split("\n").slice(1)).toEqual(
        body.slice(1, 1 + CUE_BODY_LINES),
      );
      // Scrolled to the end: the last CUE_BODY_LINES rows, flush to the bottom.
      const end = cueText(long, undefined, max).split("\n").slice(1);
      expect(end).toEqual(body.slice(max, max + CUE_BODY_LINES));
      expect(end[end.length - 1]).toBe(body[body.length - 1]);
      // An over-large scroll clamps to that same last window rather than blanking.
      expect(cueText(long, undefined, max + 99).split("\n").slice(1)).toEqual(end);
    });

    it("keeps the countdown on the title row while the body scrolls", () => {
      const rows = cueText(long, 4, 2).split("\n");
      expect(rows[0]).toMatch(/^HISTORY {2,}4s$/);
      expect(getTextWidth(rows[0])).toBeLessThanOrEqual(CUE_TEXT_W);
      expect(rows.slice(1)).toEqual(cueBodyLines(long.body).slice(2, 2 + CUE_BODY_LINES));
    });
  });

  describe("box shrinks to a short body (XERK-119)", () => {
    // A short cue: title + one body row.
    const oneLine = { title: "Sun", body: "Close star." };
    // A body that wraps to exactly two rows — under the CUE_BODY_LINES cap.
    const twoLine = { title: "Note", body: Array.from({ length: 12 }, (_, i) => `word${i}`).join(" ") };
    // A body that overflows the three-row window and scrolls.
    const long = {
      title: "History",
      body: Array.from({ length: 60 }, (_, i) => `word${i}`).join(" "),
    };

    it("cueRows counts the title over the visible body rows, capped at the window", () => {
      expect(cueBodyLines(oneLine.body)).toHaveLength(1);
      expect(cueRows(oneLine)).toBe(2); // title + one body row
      expect(cueBodyLines(twoLine.body)).toHaveLength(2);
      expect(cueRows(twoLine)).toBe(3); // title + two body rows
      expect(cueBodyLines(long.body).length).toBeGreaterThan(CUE_BODY_LINES);
      expect(cueRows(long)).toBe(CUE_ROWS); // capped: title + the full window
    });

    it("sizes the box height to the cue's rows, whole caption rows only", () => {
      for (const cue of [oneLine, twoLine, long]) {
        const box = cueBox(cue);
        const rows = cueRows(cue);
        expect(box.height).toBe(cueHeight(rows));
        // Ends on a whole transcript-row boundary — no half-line the host scrolls.
        expect(box.height).toBeLessThanOrEqual((rows + 1) * LINE_H);
        expect(box.height).toBeGreaterThan(rows * LINE_H);
      }
      // A shorter body makes a strictly shorter box.
      expect(cueBox(oneLine).height).toBeLessThan(cueBox(twoLine).height);
      expect(cueBox(twoLine).height).toBeLessThan(cueBox(long).height);
      expect(cueBox(long).height).toBe(CUE_H);
    });

    it("masks only as many caption rows as the box is tall, freeing the rest", () => {
      const text = Array.from({ length: 30 }, (_, i) => `line ${i}`).join("\n");
      const plain = fitCaptionRows(text);
      const [shortFirst, shortLast] = cueRowRange(oneLine);
      const [, longLast] = cueRowRange(long);
      // The short cue masks strictly fewer rows than a full-height one.
      expect(shortLast).toBeLessThan(longLast);
      expect(shortLast).toBe(CUE_ROW_LAST - (CUE_ROWS - cueRows(oneLine)));

      const rows = occludedCaption(text, shortFirst, shortLast).split("\n");
      for (let r = 0; r < CAPTION_LINES; r++) {
        if (r >= shortFirst && r <= shortLast) expect(rows[r]).toBe("");
        // The rows a full box would have masked but this short one does not: the
        // live transcript shows through (XERK-119).
        else expect(rows[r]).toBe(plain[r]);
      }
      // The rows freed relative to a full-height box carry live text.
      for (let r = shortLast + 1; r <= CUE_ROW_LAST; r++) {
        expect(rows[r]).toBe(plain[r]);
        expect(rows[r]).not.toBe("");
      }
    });

    it("still covers the whole overlap of the (shorter) box with the band", () => {
      const [first, last] = cueRowRange(oneLine);
      const box = cueBox(oneLine);
      const maskTop = LINE_H + first * LINE_H;
      const maskBottom = LINE_H + (last + 1) * LINE_H;
      expect(maskTop).toBeLessThanOrEqual(Math.max(box.y, LINE_H));
      expect(maskBottom).toBeGreaterThanOrEqual(box.y + box.height);
    });
  });
});

describe("cue source on the lens (XERK-120)", () => {
  it("appends a grounded cue's source to the scrollable body", () => {
    const grounded = { title: "PM", body: "Andy Burnham took office.", source: "BBC News" };
    expect(cueDetail(grounded)).toBe("Andy Burnham took office. — BBC News");
    // The attribution rides the laid-out text, so it scrolls with the body end.
    expect(cueText(grounded)).toContain("BBC News");
  });

  it("leaves an ungrounded cue's body untouched", () => {
    const cue = { title: "Sun", body: "About 150 million km away." };
    expect(cueDetail(cue)).toBe(cue.body);
    expect(cueText(cue)).not.toContain("—");
  });

  it("keeps box height, masked rows and scroll range agreeing on the appended source", () => {
    // A body sized so the appended source pushes it over a row boundary: every
    // consumer must lay out the SAME composed string, or the box would clip or
    // blank rows (XERK-119's invariant, now with the source appended).
    const cue = {
      title: "History",
      body: "word ".repeat(30).trim(),
      source: "Wikipedia",
    };
    const rows = cueText(cue).split("\n");
    expect(rows).toHaveLength(cueRows(cue));
    expect(cueBox(cue).height).toBe(cueHeight(cueRows(cue)));
    // Scrolled to the end, the last visible row carries the attribution.
    const rowsAtEnd = cueText(cue, undefined, cueMaxScroll(cueDetail(cue))).split("\n");
    expect(rowsAtEnd[rowsAtEnd.length - 1]).toContain("Wikipedia");
  });
});
