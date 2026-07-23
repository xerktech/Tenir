/**
 * Phone-side Session page (XERK-93): mirrors the running glasses session
 * full-page in real time — an explanatory idle state outside a session, honest
 * about connectivity inside one, newest text followed.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  SessionPage,
  querySessionPageElements,
  sessionStatus,
  type LiveSessionView,
} from "../src/phone/session";

function mountDom(): void {
  document.body.innerHTML = `
    <section id="page-session">
      <span id="session-dot" hidden></span>
      <span class="badge-neutral" id="session-badge">idle</span>
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
    ...overrides,
  };
}

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

describe("sessionStatus", () => {
  it("is honest about connectivity, like the lens (XERK-82)", () => {
    expect(sessionStatus({ connection: "open" })).toBe("listening");
    expect(sessionStatus({ connection: "connecting" })).toBe("connecting…");
    expect(sessionStatus({ connection: "closed" })).toBe("reconnecting…");
  });
});
