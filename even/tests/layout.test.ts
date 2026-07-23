import { getTextWidth, measureTextWrap } from "@evenrealities/pretext";
import { describe, expect, it } from "vitest";

import {
  buildMainPage,
  buildMenuPage,
  buildStartupContainer,
  CAPTION_H,
  CAPTION_LINES,
  CLOCK_W,
  clockText,
  CONTAINER,
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

  it("the masked rows cover the box's whole vertical extent", () => {
    const maskTop = LINE_H + MENU_ROW_FIRST * LINE_H;
    const maskBottom = LINE_H + (MENU_ROW_LAST + 1) * LINE_H;
    expect(maskTop).toBeLessThanOrEqual(MENU_Y);
    expect(maskBottom).toBeGreaterThanOrEqual(MENU_Y + MENU_H);
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

  it("centers the box horizontally within the caption band", () => {
    const menu = menuOf(buildMenuPage(CONTENTS, "continue"));
    expect(menu.xPosition! * 2 + menu.width!).toBe(SCREEN_W); // centered
    expect(menu.yPosition!).toBeGreaterThanOrEqual(LINE_H); // below the status line
    expect(menu.yPosition! + menu.height!).toBeLessThanOrEqual(LINE_H + CAPTION_H); // inside the band
  });

  it("both labels fit the box interior without wrapping", () => {
    const interior = MENU_W - 2 * (MENU_PAD + MENU_BORDER);
    expect(getTextWidth("› Exit session")).toBeLessThanOrEqual(interior);
    expect(getTextWidth("› Continue")).toBeLessThanOrEqual(interior);
  });
});
