/**
 * Lens controller (XERK-85): the glasses-UI session state machine, exercised
 * end to end with a stub bridge, a fake api client, and fake timers — click
 * starts/stops a session, the status line reads "listening" with moving dots,
 * the clock shows the current time, captions stay fitted to the band, and the
 * phone-side transcript strip mirrors it all in real time.
 */

import { OsEventTypeList, type EvenAppBridge, type EvenHubEvent } from "@evenrealities/even_hub_sdk";
import type { ApiHandlers, SessionParams } from "@tenir/client-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemStorage } from "./memStorage";

let controllerMod: typeof import("../src/lens/controller");
let layout: typeof import("../src/lens/layout");
let transcriptMod: typeof import("../src/phone/transcript");
let cfg: typeof import("../src/config");

beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 6, 22, 14, 5));
  // config.ts and client-core carry module state; reset so initConfig runs fresh.
  vi.resetModules();
  controllerMod = await import("../src/lens/controller");
  layout = await import("../src/lens/layout");
  transcriptMod = await import("../src/phone/transcript");
  cfg = await import("../src/config");
  await cfg.initConfig(new MemStorage());
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

/** Let queued microtasks + due timers run (the writer pump is microtask-driven). */
const settle = () => vi.advanceTimersByTimeAsync(0);

/** A stub Even bridge: device storage, page rebuilds + the single event subscription. */
function fakeBridge(initial: Record<string, string> = {}, rebuildOk = true) {
  const store = new Map(Object.entries(initial));
  const shutdowns: number[] = [];
  const rebuilds: Array<{ containerTotalNum?: number; textObject?: unknown[] }> = [];
  let handler: ((e: EvenHubEvent) => void) | null = null;
  const bridge = {
    onEvenHubEvent: (h: (e: EvenHubEvent) => void) => {
      handler = h;
      return () => {
        handler = null;
      };
    },
    audioControl: async () => true,
    getLocalStorage: async (k: string) => store.get(k) ?? "",
    setLocalStorage: async (k: string, v: string) => {
      store.set(k, v);
      return true;
    },
    shutDownPageContainer: async () => {
      shutdowns.push(1);
      return true;
    },
    rebuildPageContainer: async (page: { containerTotalNum?: number; textObject?: unknown[] }) => {
      rebuilds.push(page);
      return rebuildOk;
    },
  } as unknown as EvenAppBridge;
  return { bridge, store, shutdowns, rebuilds, emit: (e: EvenHubEvent) => handler?.(e) };
}

/** A fake api client: records calls, exposes the handlers so tests push captions. */
function fakeClientFactory() {
  const calls: Array<{ params: SessionParams; resume?: string }> = [];
  const stops: number[] = [];
  const sent: Uint8Array[] = [];
  let handlers: ApiHandlers = {};
  const createClient = (_url: string, h: ApiHandlers) => {
    handlers = h;
    return {
      start: (params: SessionParams, resume?: string) => calls.push({ params, resume }),
      stop: () => stops.push(1),
      sendAudio: (pcm: Uint8Array) => {
        sent.push(pcm);
        return true;
      },
    };
  };
  return {
    createClient,
    calls,
    stops,
    sent,
    handlers: () => handlers,
  };
}

async function boot(
  opts: { store?: Record<string, string>; withPhone?: boolean; rebuildFails?: boolean } = {},
) {
  const { bridge, store, shutdowns, rebuilds, emit } = fakeBridge(opts.store, !opts.rebuildFails);
  const latest = new Map<number, string>();
  const writer = new layout.LensTextWriter(async (c, content) => {
    latest.set(c.id, content);
    return true;
  });
  let phone: InstanceType<typeof transcriptMod.PhoneTranscript> | null = null;
  if (opts.withPhone) {
    document.body.innerHTML = `
      <section id="live-transcript" hidden>
        <span id="live-status"></span>
        <ul id="live-text"></ul>
      </section>
    `;
    phone = new transcriptMod.PhoneTranscript(transcriptMod.queryPhoneTranscriptElements()!);
  }
  const api = fakeClientFactory();
  const controls = await controllerMod.wireLens(bridge, new MemStorage(), writer, phone, {
    createClient: api.createClient,
  });
  await settle();
  const text = (c: { id: number }) => latest.get(c.id);
  // Distinct physical gestures are spaced past the same-type dedupe window
  // (a host may mirror one gesture on both the sysEvent and textEvent channel).
  const sys = async (eventType: OsEventTypeList) => {
    emit({ sysEvent: { eventType } } as EvenHubEvent);
    await vi.advanceTimersByTimeAsync(controllerMod.GESTURE_DEDUPE_MS + 50);
  };
  const click = () => sys(OsEventTypeList.CLICK_EVENT);
  const doubleTap = () => sys(OsEventTypeList.DOUBLE_CLICK_EVENT);
  const swipeUp = () => sys(OsEventTypeList.SCROLL_TOP_EVENT);
  const swipeDown = () => sys(OsEventTypeList.SCROLL_BOTTOM_EVENT);
  /** End the running session through the popup: double tap → Exit session → tap. */
  const exitViaMenu = async () => {
    await doubleTap();
    await swipeDown();
    await click();
  };
  return {
    controls,
    api,
    emit,
    store,
    shutdowns,
    rebuilds,
    text,
    click,
    doubleTap,
    swipeUp,
    swipeDown,
    exitViaMenu,
  };
}

const C = () => layout.CONTAINER;

describe("wireLens (XERK-85: explicit session start/stop from the glasses UI)", () => {
  it("idles at 'tap to start' once signed in, clock in the corner, no session", async () => {
    const t = await boot();
    t.controls.enable();
    await settle();
    expect(t.text(C().status)).toBe("ready");
    expect(t.text(C().caption)).toBe(controllerMod.IDLE_PROMPT);
    expect(t.text(C().clock)).toBe("2:05 PM"); // the ready page shows the time too
    expect(t.api.calls).toHaveLength(0);
  });

  it("keeps the idle clock on the current minute", async () => {
    const t = await boot();
    t.controls.enable();
    await settle();
    expect(t.text(C().clock)).toBe("2:05 PM");
    vi.setSystemTime(new Date(2026, 6, 22, 14, 6));
    await vi.advanceTimersByTimeAsync(controllerMod.TICK_MS);
    expect(t.text(C().clock)).toBe("2:06 PM"); // ticks while idle, not just recording
  });

  it("a tap starts a new session; taps while recording do nothing", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    expect(t.api.calls).toHaveLength(1);
    expect(t.api.calls[0].resume).toBeUndefined(); // fresh session
    expect(t.text(C().status)).toBe("connecting to server…");

    // A brushed temple must not end a recording: single taps are inert now.
    await t.click();
    await t.click();
    expect(t.api.stops).toHaveLength(0);
    expect(t.api.calls).toHaveLength(1);
    expect(t.text(C().status)).toBe("connecting to server…");
  });

  it("ends a session only through the popup: double tap → Exit session → tap", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();

    await t.doubleTap();
    // The popup is its own bordered box on a rebuilt 5-container page.
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(5);
    expect(t.text(C().menu)).toBe("› Continue\n  Exit session"); // Continue is the default, on top
    await t.swipeDown();
    expect(t.text(C().menu)).toBe("  Continue\n› Exit session");
    await t.click();

    expect(t.api.stops).toHaveLength(1); // session.end sent, socket closed
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(4); // popup page torn back down
    expect(t.text(C().status)).toBe("ready");
    expect(t.text(C().caption)).toBe(controllerMod.IDLE_PROMPT);
    expect(t.text(C().clock)).toBe("2:05 PM"); // the clock stays up on the ready page
  });

  it("the popup swipes also work through the textEvent channel (on-device path)", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    await t.doubleTap();

    // On real glasses, gestures aimed at the captured touch overlay arrive as
    // textEvent, not sysEvent.
    t.emit({
      textEvent: { containerID: 5, eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT },
    } as EvenHubEvent);
    await settle();
    expect(t.text(C().menu)).toBe("  Continue\n› Exit session");

    await vi.advanceTimersByTimeAsync(controllerMod.GESTURE_DEDUPE_MS + 50);
    t.emit({ textEvent: { containerID: 5, eventType: OsEventTypeList.CLICK_EVENT } } as EvenHubEvent);
    await settle();
    expect(t.api.stops).toHaveLength(1); // Exit session confirmed via textEvent tap
  });

  it("falls back to an in-band menu when the popup-page rebuild fails (never stranded)", async () => {
    const t = await boot({ rebuildFails: true });
    t.controls.enable();
    await t.click();

    await t.doubleTap();
    // The popup page never appeared — the caption band carries the menu instead.
    expect(t.text(C().caption)).toBe("› Continue\n  Exit session");
    await t.swipeDown();
    expect(t.text(C().caption)).toBe("  Continue\n› Exit session");
    await t.click();

    expect(t.api.stops).toHaveLength(1); // the wearer still got out of the session
    expect(t.text(C().status)).toBe("ready");
    expect(t.text(C().caption)).toBe(controllerMod.IDLE_PROMPT);
  });

  it("a gesture mirrored on both channels is handled once", async () => {
    const t = await boot();
    t.controls.enable();
    // The same physical tap lands as sysEvent AND textEvent back to back.
    t.emit({ sysEvent: { eventType: OsEventTypeList.CLICK_EVENT } } as EvenHubEvent);
    t.emit({ textEvent: { containerID: 5, eventType: OsEventTypeList.CLICK_EVENT } } as EvenHubEvent);
    await settle();
    expect(t.api.calls).toHaveLength(1); // one session, not two
  });

  it("a mirrored confirm tap can't immediately start a new session", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    await t.doubleTap();
    await t.swipeDown(); // highlight Exit session
    // One physical tap confirming Exit session lands on both channels back to back.
    t.emit({ textEvent: { containerID: 5, eventType: OsEventTypeList.CLICK_EVENT } } as EvenHubEvent);
    t.emit({ sysEvent: { eventType: OsEventTypeList.CLICK_EVENT } } as EvenHubEvent);
    await settle();
    expect(t.api.stops).toHaveLength(1); // the session ended…
    expect(t.api.calls).toHaveLength(1); // …and the mirror did not start a new one
  });

  it("Continue (the default) dismisses the popup and keeps recording", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    t.api.handlers().onFinal?.({
      type: "caption.final",
      segmentId: "s1",
      text: "before the popup",
      startMs: 0,
      endMs: 900,
    });
    await settle();

    await t.doubleTap();
    // The conversation keeps running while the popup is up — captions render
    // with the rows the box covers masked, the rest flowing around it.
    t.api.handlers().onPartial?.({ type: "caption.partial", text: "under the popup" });
    await settle();
    expect(t.text(C().menu)).toBe("› Continue\n  Exit session");
    expect(t.text(C().caption)).toBe(layout.occludedCaption("before the popup\n› under the popup"));

    // Swiping down and back up re-highlights Continue; a tap confirms it.
    await t.swipeDown();
    await t.swipeUp();
    expect(t.text(C().menu)).toBe("› Continue\n  Exit session");
    await t.click();
    expect(t.api.stops).toHaveLength(0); // still recording
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(4); // plain page again
    const caption = t.text(C().caption)!;
    expect(caption).toContain("before the popup"); // the full-band live view is back
    expect(caption).toContain("under the popup");
    expect(caption).toBe(layout.fitCaption("before the popup\n› under the popup"));
  });

  it("a second double tap dismisses the popup, same as Continue", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    await t.doubleTap();
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(5);
    await t.doubleTap();
    expect(t.api.stops).toHaveLength(0);
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(4);
  });

  it("double tap outside a session asks the host to exit the app, not a popup", async () => {
    const t = await boot();
    await t.doubleTap(); // before sign-in
    t.controls.enable();
    await t.doubleTap(); // idle
    expect(t.shutdowns).toHaveLength(2);
    expect(t.api.calls).toHaveLength(0); // no session was started
    expect(t.text(C().caption)).toBe(controllerMod.IDLE_PROMPT);
  });

  it("ignores clicks before sign-in", async () => {
    const t = await boot();
    await t.click();
    expect(t.api.calls).toHaveLength(0);
  });

  it("shows the clock and the moving listening dots while recording", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    expect(t.text(C().clock)).toBe("2:05 PM"); // current time, top right

    t.api.handlers().onConnectionChange?.("open");
    await settle();
    await vi.advanceTimersByTimeAsync(controllerMod.TICK_MS);
    expect(t.text(C().status)).toBe("listening..");
    await vi.advanceTimersByTimeAsync(controllerMod.TICK_MS);
    expect(t.text(C().status)).toBe("listening...");
    await vi.advanceTimersByTimeAsync(controllerMod.TICK_MS);
    expect(t.text(C().status)).toBe("listening.");

    // The clock follows the minute.
    vi.setSystemTime(new Date(2026, 6, 22, 14, 6));
    await vi.advanceTimersByTimeAsync(controllerMod.TICK_MS);
    expect(t.text(C().clock)).toBe("2:06 PM");
  });

  it("renders captions fitted to the band — bottom-anchored, old text dropped", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    t.api.handlers().onConnectionChange?.("open");

    t.api.handlers().onPartial?.({ type: "caption.partial", text: "hey th" });
    await settle();
    expect(t.text(C().caption)).toBe("\n".repeat(layout.CAPTION_LINES - 1) + "› hey th");

    for (let i = 0; i < 30; i++) {
      t.api.handlers().onFinal?.({
        type: "caption.final",
        segmentId: `s${i}`,
        text: `turn number ${i}`,
        startMs: i * 1000,
        endMs: i * 1000 + 900,
      });
    }
    await settle();
    const caption = t.text(C().caption)!;
    expect(caption.endsWith("turn number 29")).toBe(true); // newest at the bottom
    expect(caption).not.toContain("turn number 0"); // oldest fell off the top
  });

  it("streams audio only while a session records", async () => {
    const t = await boot();
    t.controls.enable();
    const frame = { audioEvent: { audioPcm: [1, 2, 3] } } as unknown as EvenHubEvent;
    t.emit(frame);
    expect(t.api.sent).toHaveLength(0); // idle: no session to feed

    await t.click();
    t.emit(frame);
    expect(t.api.sent).toHaveLength(1);

    await t.exitViaMenu(); // stop
    t.emit(frame);
    expect(t.api.sent).toHaveLength(1);
  });

  it("persists the running session and clears it on stop", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    t.api.handlers().onReady?.({ type: "session.ready", sessionId: "sess-1" });
    await vi.advanceTimersByTimeAsync(2000); // past the persist debounce
    expect(JSON.parse(t.store.get("tenir.session")!)).toMatchObject({ sessionId: "sess-1" });

    await t.exitViaMenu(); // stop — the session is over, nothing to resume
    expect(t.store.get("tenir.session")).toBe("");
  });

  it("resumes a persisted mid-session recording on sign-in", async () => {
    const t = await boot({
      store: {
        "tenir.session": JSON.stringify({
          sessionId: "sess-9",
          micSource: "g2-microphone",
          transcript: "earlier words",
        }),
      },
    });
    t.controls.enable();
    await settle();
    expect(t.api.calls).toHaveLength(1);
    expect(t.api.calls[0].resume).toBe("sess-9");
    expect(t.text(C().caption)!.endsWith("earlier words")).toBe(true);
  });

  it("mirrors the session to the phone transcript strip in real time", async () => {
    const t = await boot({ withPhone: true });
    const panel = () => document.getElementById("live-transcript")!;
    t.controls.enable();
    await settle();
    expect(panel().hidden).toBe(true); // idle: no strip

    await t.click();
    t.api.handlers().onConnectionChange?.("open");
    t.api.handlers().onFinal?.({
      type: "caption.final",
      segmentId: "s1",
      text: "hello phone",
      startMs: 0,
      endMs: 900,
    });
    t.api.handlers().onPartial?.({ type: "caption.partial", text: "and mo" });
    await settle();
    expect(panel().hidden).toBe(false);
    expect(document.getElementById("live-status")!.textContent).toBe("listening");
    const rows = [...document.querySelectorAll("#live-text li")].map((li) => li.textContent);
    expect(rows).toEqual(["hello phone", "and mo"]);

    await t.exitViaMenu(); // stop
    expect(panel().hidden).toBe(true);
  });

  it("stops the session and shows the sign-in prompt on sign-out", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    t.controls.disable();
    await settle();
    expect(t.api.stops).toHaveLength(1);
    expect(t.text(C().status)).toBe("not signed in");
    expect(t.text(C().caption)).toBe(controllerMod.SIGN_IN_PROMPT);
    await t.click();
    expect(t.api.calls).toHaveLength(1); // disabled: the click is ignored
  });
});
