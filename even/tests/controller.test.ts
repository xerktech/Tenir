/**
 * Lens controller (XERK-85): the glasses-UI session state machine, exercised
 * end to end with a stub bridge, a fake api client, and fake timers — click
 * starts/stops a session, the status line reads "listening" with moving dots,
 * the clock shows the current time, captions stay fitted to the band, and the
 * phone-side Session page mirrors it all in real time (XERK-93).
 */

import { OsEventTypeList, type EvenAppBridge, type EvenHubEvent } from "@evenrealities/even_hub_sdk";
import type { ApiHandlers, SessionParams } from "@tenir/client-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemStorage } from "./memStorage";

let controllerMod: typeof import("../src/lens/controller");
let layout: typeof import("../src/lens/layout");
let sessionMod: typeof import("../src/phone/session");
let cfg: typeof import("../src/config");

beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 6, 22, 14, 5));
  // config.ts and client-core carry module state; reset so initConfig runs fresh.
  vi.resetModules();
  controllerMod = await import("../src/lens/controller");
  layout = await import("../src/lens/layout");
  sessionMod = await import("../src/phone/session");
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
  let phone: InstanceType<typeof sessionMod.SessionPage> | null = null;
  if (opts.withPhone) {
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
    phone = new sessionMod.SessionPage(sessionMod.querySessionPageElements()!);
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
    // The popup is its own bordered strip on a rebuilt 5-container page.
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(5);
    expect(t.text(C().menu)).toBe("› Continue\n  Exit session"); // Continue is the default, on top
    // The strip covers the status line and clock: both blank while it is up.
    expect(t.text(C().status)).toBe("");
    expect(t.text(C().clock)).toBe("");
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

  it("keeps the clock and listening dots live while the app is backgrounded (XERK-113)", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    t.api.handlers().onConnectionChange?.("open");
    await settle();
    await vi.advanceTimersByTimeAsync(controllerMod.TICK_MS);
    expect(t.text(C().status)).toBe("listening..");

    // Wearer pockets the phone: the glasses keep showing the lens over BLE, so
    // the dots must still move and the clock must still follow the minute.
    t.emit({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_EXIT_EVENT } } as EvenHubEvent);
    await vi.advanceTimersByTimeAsync(controllerMod.TICK_MS);
    expect(t.text(C().status)).toBe("listening..."); // dots advanced while backgrounded
    await vi.advanceTimersByTimeAsync(controllerMod.TICK_MS);
    expect(t.text(C().status)).toBe("listening.");

    vi.setSystemTime(new Date(2026, 6, 22, 14, 7));
    await vi.advanceTimersByTimeAsync(controllerMod.TICK_MS);
    expect(t.text(C().clock)).toBe("2:07 PM"); // clock still follows the minute
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

  it("resumes a backgrounded mid-session recording on sign-in (XERK-117)", async () => {
    const t = await boot({
      store: {
        "tenir.session": JSON.stringify({
          sessionId: "sess-9",
          micSource: "g2-microphone",
          transcript: "earlier words",
          resumable: true, // persisted while backgrounded — the app was not closed
        }),
      },
    });
    t.controls.enable();
    await settle();
    expect(t.api.calls).toHaveLength(1);
    expect(t.api.calls[0].resume).toBe("sess-9");
    expect(t.text(C().caption)!.endsWith("earlier words")).toBe(true);
  });

  it("does NOT resume a session the app was closed on — idles instead (XERK-117)", async () => {
    // A snapshot left behind by a close/kill is persisted from the foreground, so
    // it carries no resumable flag. The server has finalized it to history; the
    // glasses must start fresh rather than reopen it.
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
    expect(t.api.calls).toHaveLength(0); // no resume, no new session
    expect(t.text(C().caption)).toBe(controllerMod.IDLE_PROMPT);
    expect(t.store.get("tenir.session")).toBe(""); // the stale remnant is cleared
  });

  it("ends and clears the session when the app is closed, not just backgrounded (XERK-117)", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    t.api.handlers().onReady?.({ type: "session.ready", sessionId: "sess-1" });
    await vi.advanceTimersByTimeAsync(2000); // past the persist debounce

    // The app closes (SYSTEM_EXIT), distinct from backgrounding: the session is
    // ended (session.end → the api finalizes it to history) and its snapshot is
    // cleared so the next boot won't resume it.
    t.emit({ sysEvent: { eventType: OsEventTypeList.SYSTEM_EXIT_EVENT } } as EvenHubEvent);
    await settle();
    expect(t.api.stops).toHaveLength(1); // client.stop() → session.end
    expect(t.store.get("tenir.session")).toBe("");
  });

  it("marks the session resumable when backgrounded, so the next boot restores it (XERK-117)", async () => {
    const t = await boot();
    t.controls.enable();
    await t.click();
    t.api.handlers().onReady?.({ type: "session.ready", sessionId: "sess-2" });
    await vi.advanceTimersByTimeAsync(2000);
    // Foreground snapshot is not resumable.
    expect(JSON.parse(t.store.get("tenir.session")!).resumable).toBe(false);

    // Backgrounding flushes a resumable snapshot immediately.
    t.emit({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_EXIT_EVENT } } as EvenHubEvent);
    await settle();
    const snap = JSON.parse(t.store.get("tenir.session")!);
    expect(snap).toMatchObject({ sessionId: "sess-2", resumable: true });

    // Coming back to the foreground flips it back so a later kill won't resume it.
    t.emit({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_ENTER_EVENT } } as EvenHubEvent);
    await vi.advanceTimersByTimeAsync(2000);
    expect(JSON.parse(t.store.get("tenir.session")!).resumable).toBe(false);
  });

  it("mirrors the session to the phone Session page in real time", async () => {
    const t = await boot({ withPhone: true });
    const badge = () => document.getElementById("session-badge")!;
    const emptyTitle = () => document.getElementById("session-empty-title")!;
    t.controls.enable();
    await settle();
    expect(badge().textContent).toBe("idle"); // idle: the page explains itself
    expect(emptyTitle().textContent).toBe("No session running");

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
    expect(badge().textContent).toBe("listening");
    const rows = [...document.querySelectorAll("#session-text li")].map((li) => li.textContent);
    expect(rows).toEqual(["hello phone", "and mo"]);

    await t.exitViaMenu(); // stop
    expect(badge().textContent).toBe("idle");
    expect(document.getElementById("session-text")!.hidden).toBe(true);
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

describe("wireLens cues (XERK-81)", () => {
  const CUE = {
    type: "cue" as const,
    cueId: "c1",
    title: "Sun",
    body: "About 150 million km away.",
    atMs: 1000,
  };
  /** Enable, start a session, and return the driver. */
  const record = async () => {
    const t = await boot();
    t.controls.enable();
    await settle();
    await t.click(); // idle → a tap starts a session
    await settle();
    return t;
  };

  it("shows a cue in the bordered popup above the transcript, then dismisses it after the TTL", async () => {
    const t = await record();
    t.api.handlers().onCue?.(CUE);
    await vi.advanceTimersByTimeAsync(50);
    // The cue rides the shared popup strip, rebuilt on top of the base page.
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 10));
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(5);

    // Auto-dismissed after ~10s: the page rebuilds back to the plain layout.
    await vi.advanceTimersByTimeAsync(controllerMod.CUE_TTL_MS + 50);
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(4);
  });

  it("lets the double-tap menu take the popup over from a showing cue", async () => {
    const t = await record();
    t.api.handlers().onCue?.(CUE);
    await vi.advanceTimersByTimeAsync(50);
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 10));

    await t.doubleTap(); // open the menu — it owns the shared popup
    expect(t.text(C().menu)).toBe("› Continue\n  Exit session");
  });

  it("queues a cue that arrives while the menu is open, then shows it once the menu closes (XERK-102)", async () => {
    const t = await record();
    await t.doubleTap(); // menu up
    expect(t.text(C().menu)).toBe("› Continue\n  Exit session");
    t.api.handlers().onCue?.(CUE);
    await vi.advanceTimersByTimeAsync(50);
    // The interactive menu is untouched — the cue waits its turn behind it.
    expect(t.text(C().menu)).toBe("› Continue\n  Exit session");

    await t.doubleTap(); // dismiss the menu — the queued cue now gets the popup
    await vi.advanceTimersByTimeAsync(50);
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 10));
  });

  it("queues a cue that arrives while another is up and pops it after the first's TTL (XERK-102)", async () => {
    const t = await record();
    const CUE2 = { ...CUE, cueId: "c2", title: "Moon", body: "About 384,400 km away." };
    t.api.handlers().onCue?.(CUE);
    await vi.advanceTimersByTimeAsync(50);
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 10));

    // Second cue arrives while the first is still up: it must not clobber it.
    t.api.handlers().onCue?.(CUE2);
    await vi.advanceTimersByTimeAsync(50);
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 10));

    // When the first is released at its TTL, the second pops immediately.
    await vi.advanceTimersByTimeAsync(controllerMod.CUE_TTL_MS);
    expect(t.text(C().menu)).toBe(layout.cueText(CUE2, 10));
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(5);

    // And the second runs its own full TTL before the box frees.
    await vi.advanceTimersByTimeAsync(controllerMod.CUE_TTL_MS + 50);
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(4);
  });

  it("mirrors the active cue to the phone Session page", async () => {
    const { bridge, emit } = fakeBridge();
    const latest = new Map<number, string>();
    const writer = new layout.LensTextWriter(async (c, content) => {
      latest.set(c.id, content);
      return true;
    });
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
      </section>`;
    const phone = new sessionMod.SessionPage(sessionMod.querySessionPageElements()!);
    const api = fakeClientFactory();
    const controls = await controllerMod.wireLens(bridge, new MemStorage(), writer, phone, {
      createClient: api.createClient,
    });
    await settle();
    controls.enable();
    await settle();
    emit({ sysEvent: { eventType: OsEventTypeList.CLICK_EVENT } } as EvenHubEvent); // start
    await vi.advanceTimersByTimeAsync(controllerMod.GESTURE_DEDUPE_MS + 50);

    const cueEl = document.getElementById("session-cue")!;
    expect(cueEl.hidden).toBe(true);
    api.handlers().onCue?.(CUE);
    await vi.advanceTimersByTimeAsync(50);
    expect(cueEl.hidden).toBe(false);
    expect(cueEl.textContent).toContain("Sun");
    expect(cueEl.textContent).toContain("150 million");
  });

  it("counts the cue's remaining seconds down on the lens (XERK-110)", async () => {
    const t = await record();
    t.api.handlers().onCue?.(CUE);
    await vi.advanceTimersByTimeAsync(50);
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 10));

    // The activity ticker advances the count. It repaints on TICK_MS, so the
    // lens can trail the true count by up to one tick — each checkpoint below
    // is therefore read at least TICK_MS into the second it asserts.
    await vi.advanceTimersByTimeAsync(1650); // ~1.7s in
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 9));
    await vi.advanceTimersByTimeAsync(3000); // ~4.7s in
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 6));
    // Still 1s through the final second, and the box is gone by the time the
    // count would read 0.
    await vi.advanceTimersByTimeAsync(5000); // ~9.7s in
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 1));
    await vi.advanceTimersByTimeAsync(300 + controllerMod.TICK_MS);
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(4);
  });

  it("keeps a cue counting down on the lens while the app is backgrounded (XERK-113)", async () => {
    const t = await record();
    // A first cue counts down normally while foregrounded, then dismisses.
    t.api.handlers().onCue?.(CUE);
    await vi.advanceTimersByTimeAsync(1650); // ~1.7s in
    expect(t.text(C().menu)).toBe(layout.cueText(CUE, 9));
    await vi.advanceTimersByTimeAsync(controllerMod.CUE_TTL_MS);

    // The wearer pockets the phone: the app backgrounds, but the glasses keep
    // showing the lens over BLE. A cue that arrives now must still count down —
    // the auto-dismiss timer runs regardless, so its number has to as well.
    t.emit({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_EXIT_EVENT } } as EvenHubEvent);
    const CUE2 = { ...CUE, cueId: "c2", title: "Moon", body: "About 384,400 km away." };
    t.api.handlers().onCue?.(CUE2);
    await vi.advanceTimersByTimeAsync(50);
    expect(t.text(C().menu)).toBe(layout.cueText(CUE2, 10)); // shows on the lens

    // Before XERK-113 the count froze here at 10s until the cue vanished.
    await vi.advanceTimersByTimeAsync(1650); // ~1.7s in
    expect(t.text(C().menu)).toBe(layout.cueText(CUE2, 9));
    await vi.advanceTimersByTimeAsync(3000); // ~4.7s in
    expect(t.text(C().menu)).toBe(layout.cueText(CUE2, 6));
  });

  it("counts down on the phone Session page too (XERK-110)", async () => {
    const { bridge, emit } = fakeBridge();
    const writer = new layout.LensTextWriter(async () => true);
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
      </section>`;
    const phone = new sessionMod.SessionPage(sessionMod.querySessionPageElements()!);
    const api = fakeClientFactory();
    const controls = await controllerMod.wireLens(bridge, new MemStorage(), writer, phone, {
      createClient: api.createClient,
    });
    await settle();
    controls.enable();
    await settle();
    emit({ sysEvent: { eventType: OsEventTypeList.CLICK_EVENT } } as EvenHubEvent); // start
    await vi.advanceTimersByTimeAsync(controllerMod.GESTURE_DEDUPE_MS + 50);

    api.handlers().onCue?.(CUE);
    await vi.advanceTimersByTimeAsync(50);
    const countdown = () =>
      document.querySelector("#session-cue .session-cue-countdown")?.textContent;
    expect(countdown()).toBe("10s");

    // Read a tick past the second boundary — the ticker drives this too.
    await vi.advanceTimersByTimeAsync(2650); // ~2.7s in
    expect(countdown()).toBe("8s");
    // The transcript is not rebuilt for a countdown tick — only the number moves.
    expect(document.getElementById("session-text")!.hidden).toBe(true);

    // Gone with the cue when it is dismissed.
    await vi.advanceTimersByTimeAsync(controllerMod.CUE_TTL_MS);
    expect(document.getElementById("session-cue")!.hidden).toBe(true);
  });

  it("embeds a released cue into the phone transcript for review (XERK-108)", async () => {
    const { bridge, emit } = fakeBridge();
    const writer = new layout.LensTextWriter(async () => true);
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
      </section>`;
    const phone = new sessionMod.SessionPage(sessionMod.querySessionPageElements()!);
    const api = fakeClientFactory();
    const controls = await controllerMod.wireLens(bridge, new MemStorage(), writer, phone, {
      createClient: api.createClient,
    });
    await settle();
    controls.enable();
    await settle();
    emit({ sysEvent: { eventType: OsEventTypeList.CLICK_EVENT } } as EvenHubEvent); // start
    await vi.advanceTimersByTimeAsync(controllerMod.GESTURE_DEDUPE_MS + 50);

    // A turn, then a cue triggered by it; the cue rides the live band.
    api.handlers().onFinal?.({
      type: "caption.final",
      segmentId: "s1",
      text: "the sun is far",
      startMs: 0,
      endMs: 900,
    });
    api.handlers().onCue?.(CUE);
    await vi.advanceTimersByTimeAsync(50);
    const box = document.getElementById("session-text")!;
    // While it's still in the band it is not yet embedded in the transcript.
    expect(box.querySelector("li.session-cue-line")).toBeNull();

    // After its TTL the cue leaves the band and drops into the transcript as an
    // inline dropdown, anchored after the turn that triggered it.
    await vi.advanceTimersByTimeAsync(controllerMod.CUE_TTL_MS + 50);
    expect(document.getElementById("session-cue")!.hidden).toBe(true); // gone from the band
    const cueLine = box.querySelector("li.session-cue-line");
    expect(cueLine).not.toBeNull();
    expect(cueLine!.querySelector(".cue-inline-title")!.textContent).toBe("Sun");
    // It sits after the spoken turn, and its body is collapsed by default.
    const lis = [...box.querySelectorAll("li")];
    expect(lis[0].textContent).toBe("the sun is far");
    expect(lis[1].classList.contains("session-cue-line")).toBe(true);
    expect(cueLine!.querySelector<HTMLElement>(".cue-inline-body")!.hidden).toBe(true);
  });
});

describe("wireLens cue scrolling (XERK-112)", () => {
  // A body that wraps well past the box's three visible rows.
  const LONG = {
    type: "cue" as const,
    cueId: "long1",
    title: "History",
    body: Array.from({ length: 60 }, (_, i) => `word${i}`).join(" "),
    atMs: 1000,
  };
  const SHORT = {
    type: "cue" as const,
    cueId: "short1",
    title: "Sun",
    body: "About 150 million km away.",
    atMs: 1000,
  };
  const record = async () => {
    const t = await boot();
    t.controls.enable();
    await settle();
    await t.click();
    await settle();
    return t;
  };

  it("scrolls a long cue's body a row at a time, both ways, clamped to the body", async () => {
    const t = await record();
    // Sanity: this body really does overflow the box (so there is something to scroll).
    expect(layout.cueMaxScroll(LONG.body)).toBeGreaterThanOrEqual(2);

    t.api.handlers().onCue?.(LONG);
    await vi.advanceTimersByTimeAsync(50);
    expect(t.text(C().menu)).toBe(layout.cueText(LONG, 10, 0)); // starts at the top

    // Swipe down walks the window toward the end; each swipe resets the count.
    await t.swipeDown();
    expect(t.text(C().menu)).toBe(layout.cueText(LONG, 10, 1));
    await t.swipeDown();
    expect(t.text(C().menu)).toBe(layout.cueText(LONG, 10, 2));

    // Swipe up walks it back toward the start, and never past the top.
    await t.swipeUp();
    expect(t.text(C().menu)).toBe(layout.cueText(LONG, 10, 1));
    await t.swipeUp();
    await t.swipeUp();
    expect(t.text(C().menu)).toBe(layout.cueText(LONG, 10, 0)); // clamped at 0
  });

  it("ignores swipes on a cue whose body already fits", async () => {
    const t = await record();
    expect(layout.cueMaxScroll(SHORT.body)).toBe(0);

    t.api.handlers().onCue?.(SHORT);
    await vi.advanceTimersByTimeAsync(50);
    const before = t.text(C().menu);
    await t.swipeDown();
    await t.swipeUp();
    expect(t.text(C().menu)).toBe(before); // nothing scrolled
  });

  it("a swipe resets the auto-dismiss so a long cue can be read past its TTL", async () => {
    const t = await record();
    t.api.handlers().onCue?.(LONG);
    await vi.advanceTimersByTimeAsync(50);

    // Read on: a swipe roughly 8s in restarts the dismiss timer.
    await vi.advanceTimersByTimeAsync(controllerMod.CUE_TTL_MS - 2000);
    await t.swipeDown();

    // Past the ORIGINAL TTL, the box is still up — the swipe bought it more time.
    await vi.advanceTimersByTimeAsync(3000);
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(5);

    // A full fresh TTL after the swipe, it finally auto-dismisses.
    await vi.advanceTimersByTimeAsync(controllerMod.CUE_TTL_MS);
    expect(t.rebuilds[t.rebuilds.length - 1]?.containerTotalNum).toBe(4);
  });

  it("each freshly-surfaced cue starts at the top of its own body", async () => {
    const t = await record();
    const CUE2 = { ...LONG, cueId: "long2", title: "Geography" };
    t.api.handlers().onCue?.(LONG);
    await vi.advanceTimersByTimeAsync(50);
    // Queue a second cue behind the first, then scroll the first down.
    t.api.handlers().onCue?.(CUE2);
    await t.swipeDown();
    await t.swipeDown();
    expect(t.text(C().menu)).toBe(layout.cueText(LONG, 10, 2));

    // When the first releases at its TTL, the second pops at the top, not at 2.
    await vi.advanceTimersByTimeAsync(controllerMod.CUE_TTL_MS + 50);
    expect(t.text(C().menu)).toBe(layout.cueText(CUE2, 10, 0));
  });
});
