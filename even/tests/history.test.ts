/**
 * Phone-side History page (XERK-93): the stored-session list (search, empty and
 * error states) and the conversation detail (transcript with timing, retained
 * audio, arm-then-confirm delete) — driven through an injected api fake.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation, ConversationSummary } from "@tenir/client-core";

import {
  PhoneHistory,
  errText,
  formatDuration,
  queryPhoneHistoryElements,
  segmentTiming,
  type HistoryApi,
} from "../src/phone/history";

/** The slice of index.html the history page drives. */
function mountDom(): void {
  document.body.innerHTML = `
    <div id="page-history">
      <section id="history-list">
        <form id="history-search">
          <input id="history-query" type="text" />
          <button type="submit">Search</button>
        </form>
        <div id="history-status"></div>
        <ul id="history-rows"></ul>
      </section>
      <div id="history-detail" hidden>
        <button id="history-back" type="button">← History</button>
        <button id="history-delete" type="button">Delete</button>
        <p id="history-meta"></p>
        <div id="history-transcript"></div>
        <div id="history-audio" hidden>
          <audio id="history-audio-el"></audio>
          <a id="history-audio-link">Download audio.wav</a>
        </div>
      </div>
    </div>
  `;
}

function summary(overrides: Partial<ConversationSummary> = {}): ConversationSummary {
  return {
    id: "c1",
    status: "complete",
    micSource: "g2-microphone",
    sourceLang: null,
    startedAt: "2026-07-20T10:00:00Z",
    endedAt: "2026-07-20T10:03:24Z",
    durationMs: 204_000,
    segmentCount: 12,
    hasAudio: true,
    ...overrides,
  };
}

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    ...summary(),
    segments: [
      { segmentId: "s1", text: "first turn", startMs: 3000, endMs: 7000, lang: "en" },
      { segmentId: "s2", text: "second turn", startMs: 9000, endMs: 15000, lang: "en" },
    ],
    ...overrides,
  };
}

function fakeApi(overrides: Partial<HistoryApi> = {}): HistoryApi {
  return {
    list: vi.fn(async () => [summary()]),
    get: vi.fn(async () => conversation()),
    remove: vi.fn(async () => undefined),
    audioUrl: (id: string) => `https://tenir.example.com/conversations/${id}/audio?token=t`,
    ...overrides,
  };
}

const els = () => queryPhoneHistoryElements()!;
const listSection = () => document.getElementById("history-list")!;
const detail = () => document.getElementById("history-detail")!;
const del = () => document.getElementById("history-delete")! as HTMLButtonElement;
const rowButtons = () => [...document.querySelectorAll<HTMLButtonElement>("#history-rows .history-open")];
const statusText = () => document.getElementById("history-status")!.textContent;

function mount(api: HistoryApi, deps: { onError?: (m: string) => void; disarmAfterMs?: number } = {}) {
  return new PhoneHistory(els(), { api, ...deps });
}

beforeEach(() => {
  mountDom();
  // jsdom has no media implementation; pause() would log a noisy virtual-console
  // error on every list ↔ detail switch.
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});
afterEach(() => {
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("queryPhoneHistoryElements", () => {
  it("finds the page elements", () => {
    expect(queryPhoneHistoryElements()).not.toBeNull();
  });

  it("returns null when the page has no history elements (instead of failing the app)", () => {
    document.body.innerHTML = "";
    expect(queryPhoneHistoryElements()).toBeNull();
  });
});

describe("session list", () => {
  it("lists stored sessions with date, duration, turns and status", async () => {
    const api = fakeApi();
    await mount(api).refresh();
    expect(api.list).toHaveBeenCalledWith(undefined);
    const [row] = rowButtons();
    expect(row.querySelector(".history-when")!.textContent).toBe(
      new Date("2026-07-20T10:00:00Z").toLocaleString(),
    );
    expect(row.querySelector(".history-meta")!.textContent).toBe("3:24 · 12 turns · complete");
    expect(statusText()).toBe(""); // spinner gone
  });

  it("says so when nothing was recorded yet", async () => {
    const api = fakeApi({ list: vi.fn(async () => []) });
    await mount(api).refresh();
    expect(rowButtons()).toHaveLength(0);
    expect(statusText()).toContain("No conversations yet");
  });

  it("surfaces a failed listing with a retry instead of an empty list", async () => {
    let fail = true;
    const api = fakeApi({
      list: vi.fn(async () => {
        if (fail) throw new Error("boom");
        return [summary()];
      }),
    });
    const page = mount(api);
    await page.refresh();
    expect(statusText()).toContain("Could not load history");
    expect(statusText()).toContain("boom");

    fail = false;
    document.querySelector<HTMLButtonElement>("#history-status .btn")!.click();
    await vi.waitFor(() => expect(rowButtons()).toHaveLength(1));
    expect(statusText()).toBe("");
  });

  it("searches with the entered query on submit", async () => {
    const api = fakeApi();
    mount(api);
    els().query.value = "  hello  ";
    els().form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(api.list).toHaveBeenCalledWith("hello"));
  });
});

describe("conversation detail", () => {
  it("opens a session's transcript with timing, audio and meta", async () => {
    const api = fakeApi();
    const page = mount(api);
    await page.refresh();
    rowButtons()[0].click();
    await vi.waitFor(() => expect(detail().hidden).toBe(false));
    expect(listSection().hidden).toBe(true);
    expect(api.get).toHaveBeenCalledWith("c1");

    expect(document.getElementById("history-meta")!.textContent).toBe(
      `${new Date("2026-07-20T10:00:00Z").toLocaleString()} · 3:24 · 12 turns`,
    );
    const items = [...document.querySelectorAll("#history-transcript .item")];
    expect(items.map((i) => i.textContent)).toEqual(["0:03–0:07 first turn", "0:09–0:15 second turn"]);
    expect(document.getElementById("history-audio")!.hidden).toBe(false);
    expect(document.querySelector<HTMLAudioElement>("#history-audio-el")!.src).toContain(
      "/conversations/c1/audio",
    );

    // Back returns to the list and stops holding the audio clip.
    document.getElementById("history-back")!.click();
    expect(detail().hidden).toBe(true);
    expect(listSection().hidden).toBe(false);
    expect(document.querySelector<HTMLAudioElement>("#history-audio-el")!.getAttribute("src")).toBeNull();
  });

  it("says so for a session with no transcript, and hides the player without audio", async () => {
    const api = fakeApi({
      get: vi.fn(async () => conversation({ segments: [], hasAudio: false })),
    });
    const page = mount(api);
    await page.refresh();
    rowButtons()[0].click();
    await vi.waitFor(() => expect(detail().hidden).toBe(false));
    expect(document.getElementById("history-transcript")!.textContent).toBe(
      "No transcript was recorded for this session.",
    );
    expect(document.getElementById("history-audio")!.hidden).toBe(true);
  });

  it("renders transcript text as text, not markup", async () => {
    const api = fakeApi({
      get: vi.fn(async () =>
        conversation({
          segments: [{ segmentId: "s1", text: "<b>bold?</b>", startMs: 0, endMs: 1000, lang: null }],
        }),
      ),
    });
    const page = mount(api);
    await page.refresh();
    rowButtons()[0].click();
    await vi.waitFor(() => expect(detail().hidden).toBe(false));
    expect(document.querySelector("#history-transcript b")).toBeNull();
  });

  it("toasts when a detail fails to open", async () => {
    const onError = vi.fn();
    const api = fakeApi({
      get: vi.fn(async () => {
        throw new Error("gone");
      }),
    });
    const page = mount(api, { onError });
    await page.refresh();
    rowButtons()[0].click();
    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith("Error: gone"));
    expect(detail().hidden).toBe(true);
  });
});

describe("delete (arm-then-confirm)", () => {
  async function openDetail(api: HistoryApi, deps = {}) {
    const page = mount(api, deps);
    await page.refresh();
    rowButtons()[0].click();
    await vi.waitFor(() => expect(detail().hidden).toBe(false));
    return page;
  }

  it("arms on the first click and deletes on the second, then refreshes the list", async () => {
    const api = fakeApi();
    await openDetail(api);

    del().click();
    expect(del().textContent).toBe("Confirm delete");
    expect(del().classList.contains("armed")).toBe(true);
    expect(api.remove).not.toHaveBeenCalled();

    del().click();
    expect(api.remove).toHaveBeenCalledWith("c1");
    await vi.waitFor(() => expect(detail().hidden).toBe(true));
    expect(listSection().hidden).toBe(false);
    expect(api.list).toHaveBeenCalledTimes(2); // initial + post-delete refresh
  });

  it("quietly expires an accidental first click", async () => {
    vi.useFakeTimers();
    const api = fakeApi();
    await openDetail(api, { disarmAfterMs: 4000 });

    del().click();
    expect(del().classList.contains("armed")).toBe(true);
    await vi.advanceTimersByTimeAsync(4001);
    expect(del().classList.contains("armed")).toBe(false);
    expect(del().textContent).toBe("Delete");
    expect(api.remove).not.toHaveBeenCalled();
  });

  it("toasts a failed delete", async () => {
    const onError = vi.fn();
    const api = fakeApi({
      remove: vi.fn(async () => {
        throw new Error("nope");
      }),
    });
    await openDetail(api, { onError });
    del().click();
    del().click();
    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith("Error: nope"));
  });
});

describe("reset (sign-out)", () => {
  it("drops the loaded rows, query and detail so the next user sees nothing", async () => {
    const api = fakeApi();
    const page = mount(api);
    await page.refresh();
    rowButtons()[0].click();
    await vi.waitFor(() => expect(detail().hidden).toBe(false));
    els().query.value = "secret";

    page.reset();
    expect(rowButtons()).toHaveLength(0);
    expect(statusText()).toBe("");
    expect(els().query.value).toBe("");
    expect(detail().hidden).toBe(true);
    expect(listSection().hidden).toBe(false);
  });
});

describe("formatting", () => {
  it("renders durations as m:ss and timings as offsets", () => {
    expect(formatDuration(204_000)).toBe("3:24");
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(3_723_000)).toBe("62:03"); // hours fold into minutes
    expect(segmentTiming({ segmentId: "s", text: "", startMs: 3000, endMs: 7000, lang: null })).toBe(
      "0:03–0:07",
    );
  });

  it("errText keeps api status detail and stringifies the rest", () => {
    expect(errText(new Error("x"))).toBe("Error: x");
  });
});
