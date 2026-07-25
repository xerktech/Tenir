/**
 * Phone-side Session page (XERK-93): mirrors the running glasses session
 * full-page in real time — an explanatory idle state outside a session, honest
 * about connectivity inside one, newest text followed.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  SessionPage,
  liveTranscriptRows,
  querySessionPageElements,
  sessionStatus,
  type LiveSessionView,
  type PastCue,
} from "../src/phone/session";

function mountDom(): void {
  document.body.innerHTML = `
    <section id="page-session">
      <span id="session-dot" hidden></span>
      <span class="badge-neutral" id="session-badge">idle</span>
      <div class="row" id="session-controls" hidden>
        <button class="btn btn-primary" id="session-start" type="button">Start</button>
        <button class="btn btn-danger" id="session-stop" type="button" hidden>Stop</button>
      </div>
      <div class="session-cue" id="session-cue" hidden></div>
      <div class="empty" id="session-empty">
        <p id="session-empty-title"></p>
        <p id="session-empty-hint"></p>
      </div>
      <ul id="session-text" hidden></ul>
    </section>
  `;
}

function view(overrides: Partial<LiveSessionView> = {}): LiveSessionView {
  return {
    recording: true,
    connection: "open",
    segments: [],
    partial: "",
    cue: null,
    pastCues: [],
    ...overrides,
  };
}

const pastCue = (id: string, afterIndex: number, title = id, body = `${id}-body`): PastCue => ({
  id,
  title,
  body,
  afterIndex,
});

describe("querySessionPageElements", () => {
  beforeEach(mountDom);
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("finds the page elements", () => {
    const els = querySessionPageElements();
    expect(els).not.toBeNull();
    expect(els!.text.id).toBe("session-text");
  });

  it("returns null when the page has no session elements (instead of failing the app)", () => {
    document.body.innerHTML = "";
    expect(querySessionPageElements()).toBeNull();
  });

  it("finds them in the page that actually ships", () => {
    // The test fixtures above are hand-written; this guards the real markup, so
    // a renamed or missing id can't silently drop the whole Session page (query
    // returns null → main.ts skips it) instead of failing here.
    document.body.innerHTML = readFileSync(resolve(process.cwd(), "index.html")).toString("utf8");
    const els = querySessionPageElements();
    expect(els).not.toBeNull();
    expect(els!.start.textContent).toBe("Start");
    expect(els!.stop.textContent).toBe("Stop");
    // Shipped hidden: revealed only once a lens controller attaches (XERK-116).
    expect(els!.controls.hidden).toBe(true);
  });
});

describe("SessionPage", () => {
  beforeEach(mountDom);
  afterEach(() => {
    document.body.innerHTML = "";
  });

  const mount = (callbacks = {}) => new SessionPage(querySessionPageElements()!, callbacks);
  const badge = () => document.getElementById("session-badge")!;
  const dot = () => document.getElementById("session-dot")!;
  const empty = () => document.getElementById("session-empty")!;
  const title = () => document.getElementById("session-empty-title")!;
  const hint = () => document.getElementById("session-empty-hint")!;
  const text = () => document.getElementById("session-text")!;
  const cue = () => document.getElementById("session-cue")!;
  const rows = () => [...document.querySelectorAll("#session-text li")].map((li) => li.textContent);

  it("idles with an explanation of how sessions start (and clears stale rows)", () => {
    const page = mount();
    page.update(view({ segments: ["stale"] }));
    page.update(view({ recording: false, segments: ["stale"] }));
    expect(badge().textContent).toBe("idle");
    expect(badge().className).toBe("badge-neutral");
    expect(dot().hidden).toBe(true);
    expect(empty().hidden).toBe(false);
    expect(title().textContent).toBe("No session running");
    expect(hint().textContent).toBe("Tap your glasses to start a session.");
    expect(text().hidden).toBe(true);
    expect(rows()).toEqual([]);
  });

  it("shows finalized segments plus the live partial while recording", () => {
    mount().update(view({ segments: ["first turn", "second turn"], partial: "and now th" }));
    expect(empty().hidden).toBe(true);
    expect(text().hidden).toBe(false);
    expect(rows()).toEqual(["first turn", "second turn", "and now th"]);
    expect(document.querySelector("#session-text li.partial")!.textContent).toBe("and now th");
    expect(dot().hidden).toBe(false);
    expect(badge().textContent).toBe("listening");
    expect(badge().className).toBe("badge-accent");
  });

  it("shows a waiting state before any speech arrives", () => {
    mount().update(view());
    expect(empty().hidden).toBe(false);
    expect(title().textContent).toBe("Listening for speech…");
    expect(text().hidden).toBe(true);
  });

  it("is honest about connectivity in the badge (neutral until captions flow)", () => {
    const page = mount();
    page.update(view({ connection: "connecting" }));
    expect(badge().textContent).toBe("connecting…");
    expect(badge().className).toBe("badge-neutral");
    page.update(view({ connection: "closed" }));
    expect(badge().textContent).toBe("reconnecting…");
    expect(badge().className).toBe("badge-neutral");
  });

  describe("start/stop controls (XERK-116)", () => {
    const controlsRow = () => document.getElementById("session-controls")!;
    const startBtn = () => document.getElementById("session-start") as HTMLButtonElement;
    const stopBtn = () => document.getElementById("session-stop") as HTMLButtonElement;

    it("stays hidden until a lens hands over its controls", () => {
      const page = mount();
      page.update(view({ recording: false }));
      // No glasses wired (a plain browser): nothing to record with, so no buttons.
      expect(controlsRow().hidden).toBe(true);
      expect(hint().textContent).toBe("Tap your glasses to start a session.");

      page.setControls({ start: () => {}, stop: () => {} });
      expect(controlsRow().hidden).toBe(false);
      // The row appears immediately, without waiting for the next session update.
      expect(startBtn().hidden).toBe(false);
      expect(stopBtn().hidden).toBe(true);
    });

    it("offers Start when idle and Stop while recording", () => {
      const page = mount();
      page.setControls({ start: () => {}, stop: () => {} });

      page.update(view({ recording: false }));
      expect(startBtn().hidden).toBe(false);
      expect(stopBtn().hidden).toBe(true);
      // Idle now points at the button as well as the glasses.
      expect(hint().textContent).toBe("Press Start, or tap your glasses, to begin a session.");

      page.update(view({ recording: true }));
      expect(startBtn().hidden).toBe(true);
      expect(stopBtn().hidden).toBe(false);
    });

    it("drives the controls it was given", () => {
      const start = vi.fn();
      const stop = vi.fn();
      const page = mount();
      page.setControls({ start, stop });

      page.update(view({ recording: false }));
      startBtn().click();
      expect(start).toHaveBeenCalledTimes(1);

      page.update(view({ recording: true }));
      stopBtn().click();
      expect(stop).toHaveBeenCalledTimes(1);
    });

    it("ignores clicks before any controls are attached", () => {
      mount().update(view({ recording: false }));
      expect(() => startBtn().click()).not.toThrow();
      expect(() => stopBtn().click()).not.toThrow();
    });
  });

  it("renders caption text as text, not markup", () => {
    mount().update(view({ segments: ["<b>bold?</b>"] }));
    expect(document.querySelector("#session-text b")).toBeNull();
    expect(rows()).toEqual(["<b>bold?</b>"]);
  });

  it("shows the private context cue as a titled card while recording (XERK-81)", () => {
    mount().update(view({ cue: { title: "Aptos, CA", body: "Coastal town in Santa Cruz County." } }));
    expect(cue().hidden).toBe(false);
    expect(cue().querySelector(".session-cue-title")!.textContent).toBe("Aptos, CA");
    expect(cue().querySelector(".session-cue-body")!.textContent).toBe(
      "Coastal town in Santa Cruz County.",
    );
  });

  it("shows a grounded cue's source under the body; none when ungrounded (XERK-120)", () => {
    const page = mount();
    page.update(view({ cue: { title: "PM", body: "Andy Burnham took office.", source: "BBC News" } }));
    expect(cue().querySelector(".session-cue-source")!.textContent).toBe("BBC News");
    // Ungrounded: the attribution line isn't rendered at all.
    page.update(view({ cue: { title: "Sun", body: "150M km away." } }));
    expect(cue().querySelector(".session-cue-source")).toBeNull();
  });

  it("renders the cue title/body as text, not markup", () => {
    mount().update(view({ cue: { title: "<i>x</i>", body: "<b>y</b>" } }));
    expect(cue().querySelector("i")).toBeNull();
    expect(cue().querySelector("b")).toBeNull();
    expect(cue().querySelector(".session-cue-title")!.textContent).toBe("<i>x</i>");
  });

  it("hides the cue when there is none, and when not recording (and clears it)", () => {
    const page = mount();
    page.update(view({ cue: { title: "T", body: "B" } }));
    expect(cue().hidden).toBe(false);

    // A cleared cue hides and empties the card.
    page.update(view({ cue: null }));
    expect(cue().hidden).toBe(true);
    expect(cue().textContent).toBe("");

    // A cue outside a session never shows (the card is session-private).
    page.update(view({ recording: false, cue: { title: "T", body: "B" } }));
    expect(cue().hidden).toBe(true);
    expect(cue().textContent).toBe("");
  });

  describe("cue countdown (XERK-110)", () => {
    const countdown = () => cue().querySelector(".session-cue-countdown");

    it("shows the seconds left across from the title", () => {
      mount().update(view({ cue: { title: "Aptos, CA", body: "Coastal town." }, cueSecondsLeft: 7 }));
      const head = cue().querySelector(".session-cue-head")!;
      expect(head.querySelector(".session-cue-title")!.textContent).toBe("Aptos, CA");
      expect(head.querySelector(".session-cue-countdown")!.textContent).toBe("7s");
      // The card announces itself; a number changing every second inside that
      // live region must not re-announce the whole cue, so it stays hidden.
      expect(countdown()!.getAttribute("aria-hidden")).toBe("true");
    });

    it("ticks the number without redrawing the card or the transcript", () => {
      const page = mount();
      page.update(view({ segments: ["a turn"], cue: { title: "T", body: "B" }, cueSecondsLeft: 10 }));
      const card = countdown()!;
      const firstRow = document.querySelector("#session-text li")!;

      page.tickCue(9);
      expect(countdown()!.textContent).toBe("9s");
      // Same nodes: only the text inside the countdown changed.
      expect(countdown()).toBe(card);
      expect(document.querySelector("#session-text li")).toBe(firstRow);
    });

    it("has nothing to tick once the cue is gone", () => {
      const page = mount();
      page.update(view({ cue: { title: "T", body: "B" }, cueSecondsLeft: 3 }));
      page.update(view({ cue: null }));
      expect(() => page.tickCue(2)).not.toThrow();
      expect(cue().textContent).toBe("");
    });
  });

  it("follows the newest caption inside the transcript's own scroll box (XERK-103)", () => {
    const page = mount();
    const box = text();
    // Content shorter than the box → the viewer is always "at the bottom", so
    // new captions keep it pinned there. (jsdom has no layout; drive geometry.)
    Object.defineProperty(box, "scrollHeight", { get: () => 100, configurable: true });
    Object.defineProperty(box, "clientHeight", { get: () => 300, configurable: true });
    box.scrollTop = 0;

    page.update(view({ segments: ["one", "two", "three"] }));

    // Scrolled the transcript box itself to its bottom — never the page (which
    // would carry the cue card above it out of view).
    expect(box.scrollTop).toBe(100);
  });

  it("leaves a viewer who scrolled up to re-read where they are (XERK-103)", () => {
    const page = mount();
    const box = text();
    // A tall transcript scrolled to the top: the viewer is reading earlier text.
    Object.defineProperty(box, "scrollHeight", { get: () => 600, configurable: true });
    Object.defineProperty(box, "clientHeight", { get: () => 200, configurable: true });
    box.scrollTop = 0;

    page.update(view({ segments: ["a", "b", "c", "d"] }));

    // New text must not yank them back down to the bottom.
    expect(box.scrollTop).toBe(0);
  });

  const cueLine = () => document.querySelector<HTMLElement>("#session-text li.session-cue-line");
  const cueToggle = () => cueLine()!.querySelector<HTMLButtonElement>("button.cue-inline")!;
  const cueBody = () => cueLine()!.querySelector<HTMLElement>(".cue-inline-body")!;

  it("embeds a released cue inline as a collapsed dropdown, after its turn (XERK-108)", () => {
    mount().update(
      view({
        segments: ["first turn", "second turn"],
        pastCues: [pastCue("c1", 0, "Sun", "About 150 million km away.")],
      }),
    );
    // The cue sits in the transcript list (not the pinned band), right after the
    // turn it was anchored to.
    const lis = [...document.querySelectorAll<HTMLElement>("#session-text li")];
    expect(lis.map((li) => li.className)).toEqual(["", "session-cue-line", ""]);
    expect(lis[0].textContent).toBe("first turn");
    expect(lis[2].textContent).toBe("second turn");
    expect(cueLine()!.querySelector(".cue-inline-title")!.textContent).toBe("Sun");
    const toggle = cueToggle();
    // Collapsed by default, and its body stays hidden so it doesn't crowd the
    // live feed until the viewer opens it.
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(cueBody().hidden).toBe(true);
    expect(cueBody().textContent).toBe("About 150 million km away.");
    // The still-pinned live cue band is untouched by past cues.
    expect(cue().hidden).toBe(true);
  });

  it("shows a reviewed grounded cue's source with its body (XERK-120)", () => {
    mount().update(
      view({
        segments: ["a"],
        pastCues: [{ ...pastCue("c1", 0, "PM", "Andy Burnham took office."), source: "BBC News" }],
      }),
    );
    expect(cueBody().querySelector(".cue-inline-text")!.textContent).toBe(
      "Andy Burnham took office.",
    );
    expect(cueBody().querySelector(".cue-inline-source")!.textContent).toBe("BBC News");
  });

  it("expands and collapses a reviewed cue in place on click", () => {
    mount().update(view({ segments: ["a"], pastCues: [pastCue("c1", 0, "Sun", "150M km")] }));
    const toggle = cueToggle();
    toggle.click();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(cueBody().hidden).toBe(false);
    toggle.click();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(cueBody().hidden).toBe(true);
  });

  it("keeps a reviewed cue expanded across the frequent caption redraws (XERK-108)", () => {
    const page = mount();
    page.update(view({ segments: ["a"], pastCues: [pastCue("c1", 0, "Sun", "150M km")] }));
    cueToggle().click();
    expect(cueBody().hidden).toBe(false);
    // A fresh caption rebuilds the transcript; the open cue must come back open,
    // not snap shut under the viewer.
    page.update(view({ segments: ["a", "b"], pastCues: [pastCue("c1", 0, "Sun", "150M km")] }));
    expect(cueToggle().getAttribute("aria-expanded")).toBe("true");
    expect(cueBody().hidden).toBe(false);
  });

  it("renders the reviewed cue title/body as text, not markup", () => {
    mount().update(view({ segments: ["a"], pastCues: [pastCue("c1", 0, "<i>x</i>", "<b>y</b>")] }));
    expect(cueLine()!.querySelector("i")).toBeNull();
    expect(cueLine()!.querySelector("b")).toBeNull();
    expect(cueBody().textContent).toBe("<b>y</b>");
  });

  it("fires onRecordingStart only on the idle → recording edge", () => {
    const onRecordingStart = vi.fn();
    const page = mount({ onRecordingStart });
    page.update(view({ recording: false }));
    expect(onRecordingStart).not.toHaveBeenCalled();
    page.update(view());
    expect(onRecordingStart).toHaveBeenCalledTimes(1);
    page.update(view({ segments: ["hello"] })); // still the same session
    expect(onRecordingStart).toHaveBeenCalledTimes(1);
    page.update(view({ recording: false }));
    page.update(view());
    expect(onRecordingStart).toHaveBeenCalledTimes(2);
  });
});

describe("liveTranscriptRows (XERK-108)", () => {
  const ids = (rows: ReturnType<typeof liveTranscriptRows>) =>
    rows.map((r) => (r.kind === "segment" ? r.text : `cue:${r.cue.id}`));

  it("places each reviewed cue right after the turn it was anchored to", () => {
    const rows = liveTranscriptRows(["a", "b"], [pastCue("c1", 0), pastCue("c2", 1)]);
    expect(ids(rows)).toEqual(["a", "cue:c1", "b", "cue:c2"]);
  });

  it("leads with cues anchored before any turn, or to a turn scrolled off", () => {
    const rows = liveTranscriptRows(
      ["b"],
      [pastCue("lead", -1), pastCue("gone", -3 /* anchor turn dropped */), pastCue("c", 0)],
    );
    expect(ids(rows)).toEqual(["cue:lead", "cue:gone", "b", "cue:c"]);
  });

  it("keeps release order for multiple cues sharing a turn", () => {
    const rows = liveTranscriptRows(["a"], [pastCue("c1", 0), pastCue("c2", 0)]);
    expect(ids(rows)).toEqual(["a", "cue:c1", "cue:c2"]);
  });
});

describe("sessionStatus", () => {
  it("is honest about connectivity, like the lens (XERK-82)", () => {
    expect(sessionStatus({ connection: "open" })).toBe("listening");
    expect(sessionStatus({ connection: "connecting" })).toBe("connecting…");
    expect(sessionStatus({ connection: "closed" })).toBe("reconnecting…");
  });
});
