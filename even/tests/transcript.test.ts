/**
 * Phone-side live transcript strip (XERK-85): mirrors the running glasses
 * session in real time on the phone page — hidden outside a session, honest
 * about connectivity, newest text followed.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  PhoneTranscript,
  queryPhoneTranscriptElements,
  transcriptStatus,
  type LiveTranscriptView,
} from "../src/phone/transcript";

function mountDom(): void {
  document.body.innerHTML = `
    <section id="live-transcript" hidden>
      <span id="live-status"></span>
      <ul id="live-text"></ul>
    </section>
  `;
}

function view(overrides: Partial<LiveTranscriptView> = {}): LiveTranscriptView {
  return {
    recording: true,
    connection: "open",
    segments: [],
    partial: "",
    ...overrides,
  };
}

describe("queryPhoneTranscriptElements", () => {
  beforeEach(mountDom);
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("finds the panel elements", () => {
    const els = queryPhoneTranscriptElements();
    expect(els).not.toBeNull();
    expect(els!.panel.id).toBe("live-transcript");
  });

  it("returns null when the page has no panel (instead of failing the app)", () => {
    document.body.innerHTML = "";
    expect(queryPhoneTranscriptElements()).toBeNull();
  });
});

describe("PhoneTranscript", () => {
  beforeEach(mountDom);
  afterEach(() => {
    document.body.innerHTML = "";
  });

  const mount = () => new PhoneTranscript(queryPhoneTranscriptElements()!);
  const panel = () => document.getElementById("live-transcript")!;
  const rows = () => [...document.querySelectorAll("#live-text li")].map((li) => li.textContent);

  it("stays hidden while no session is recording", () => {
    mount().update(view({ recording: false, segments: ["stale"] }));
    expect(panel().hidden).toBe(true);
  });

  it("shows finalized segments plus the live partial while recording", () => {
    mount().update(view({ segments: ["first turn", "second turn"], partial: "and now th" }));
    expect(panel().hidden).toBe(false);
    expect(rows()).toEqual(["first turn", "second turn", "and now th"]);
    expect(document.querySelector("#live-text li.partial")!.textContent).toBe("and now th");
  });

  it("shows a waiting placeholder before any speech arrives", () => {
    mount().update(view());
    expect(panel().hidden).toBe(false);
    expect(rows()).toEqual(["Listening for speech…"]);
  });

  it("renders caption text as text, not markup", () => {
    mount().update(view({ segments: ["<b>bold?</b>"] }));
    expect(document.querySelector("#live-text b")).toBeNull();
    expect(rows()).toEqual(["<b>bold?</b>"]);
  });

  it("hides again when the session stops", () => {
    const t = mount();
    t.update(view({ segments: ["hello"] }));
    expect(panel().hidden).toBe(false);
    t.update(view({ recording: false }));
    expect(panel().hidden).toBe(true);
  });
});

describe("transcriptStatus", () => {
  it("is honest about connectivity, like the lens (XERK-82)", () => {
    expect(transcriptStatus({ connection: "open" })).toBe("listening");
    expect(transcriptStatus({ connection: "connecting" })).toBe("connecting…");
    expect(transcriptStatus({ connection: "closed" })).toBe("reconnecting…");
  });
});
