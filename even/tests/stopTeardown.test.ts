import { OsEventTypeList, type EvenAppBridge, type EvenHubEvent } from "@evenrealities/even_hub_sdk";
import type { ApiHandlers } from "@tenir/client-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemStorage } from "./memStorage";

let ctl: typeof import("../src/lens/controller");
let layout: typeof import("../src/lens/layout");
let sessionMod: typeof import("../src/phone/session");
let cfg: typeof import("../src/config");
beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 6, 22, 14, 5));
  vi.resetModules();
  ctl = await import("../src/lens/controller");
  layout = await import("../src/lens/layout");
  sessionMod = await import("../src/phone/session");
  cfg = await import("../src/config");
  await cfg.initConfig(new MemStorage());
});
afterEach(() => { vi.useRealTimers(); document.body.innerHTML = ""; });
const settle = () => vi.advanceTimersByTimeAsync(0);
type Page = { containerTotalNum?: number; textObject?: Array<{ containerName?: string; content?: string }> };

/**
 * Lens teardown on stop: whatever box was up (translation, song, cue, menu) must be
 * gone from the HOST page after the session ends, by any stop path, even when a
 * rebuild fails or lands late. `host.page` models what the glasses actually show
 * (containerTotalNum: 4 = plain page, more = a popup box is up).
 */

// Host model: `mode` decides each rebuild: ok | fail | late (resolves true after 6s,
// past the BLE timeout, and the host applies it then).
async function boot() {
  let handler: ((e: EvenHubEvent) => void) | null = null;
  const rebuilds: Page[] = [];
  const host: { page: number } = { page: 4 }; // what the glasses actually show
  const ctlr = { mode: "ok" as "ok" | "fail" | "late" };
  const bridge = {
    onEvenHubEvent: (h: any) => { handler = h; return () => { handler = null; }; },
    audioControl: async () => true,
    getLocalStorage: async () => "",
    setLocalStorage: async () => true,
    shutDownPageContainer: async () => true,
    rebuildPageContainer: (page: Page) => {
      rebuilds.push(page);
      const m = ctlr.mode;
      if (m === "ok") { host.page = page.containerTotalNum!; return Promise.resolve(true); }
      if (m === "fail") return Promise.resolve(false);
      return new Promise((r) => setTimeout(() => { host.page = page.containerTotalNum!; r(true); }, 6000));
    },
  } as unknown as EvenAppBridge;
  const latest = new Map<number, string>();
  const writer = new layout.LensTextWriter(async (c, content) => { latest.set(c.id, content); return true; });
  document.body.innerHTML = `<section id="page-session"><span id="session-dot" hidden></span><span class="badge-neutral" id="session-badge">idle</span>
    <div class="row" id="session-controls" hidden><button id="session-start" type="button">Start</button><button id="session-stop" type="button" hidden>Stop</button></div>
    <div class="session-song" id="session-song" hidden></div><div class="session-cue" id="session-cue" hidden></div>
    <div class="empty" id="session-empty"><p id="session-empty-title"></p><p id="session-empty-hint"></p></div><ul id="session-text" hidden></ul></section>`;
  const phone = new sessionMod.SessionPage(sessionMod.querySessionPageElements()!);
  let handlers: ApiHandlers = {};
  const allHandlers: ApiHandlers[] = [];
  const stops: number[] = [];
  const createClient = (_u: string, h: ApiHandlers) => {
    handlers = h;
    allHandlers.push(h);
    return { start: () => {}, stop: () => stops.push(1), sendAudio: () => true };
  };
  const controls = await ctl.wireLens(bridge, new MemStorage(), writer, phone, { createClient } as any);
  await settle();
  const sys = async (eventType: OsEventTypeList) => { handler?.({ sysEvent: { eventType } } as EvenHubEvent); await vi.advanceTimersByTimeAsync(ctl.GESTURE_DEDUPE_MS + 50); };
  const t = {
    controls, rebuilds, host, ctlr, stops, h: () => handlers, allHandlers,
    text: (c: { id: number }) => latest.get(c.id),
    last: () => rebuilds[rebuilds.length - 1],
    click: () => sys(OsEventTypeList.CLICK_EVENT),
    doubleTap: () => sys(OsEventTypeList.DOUBLE_CLICK_EVENT),
    swipeDown: () => sys(OsEventTypeList.SCROLL_BOTTOM_EVENT),
    phoneStop: async () => { (document.getElementById("session-stop") as HTMLButtonElement).click(); await settle(); },
    phoneStart: async () => { (document.getElementById("session-start") as HTMLButtonElement).click(); await settle(); },
  };
  controls.enable(); await settle(); await t.click(); await settle();
  t.h().onReady?.({ type: "session.ready", sessionId: "sess1" } as any); await settle();
  return t;
}
const C = () => layout.CONTAINER;
const FINAL = { type: "caption.final", segmentId: "s1", text: "hola", lang: "es", startMs: 0, endMs: 900 } as any;
const TR = { type: "translation", segmentId: "s1", text: "hello", sourceLang: "es" } as any;
const SONG = { type: "song", songId: "song1", title: "Y", artist: "B", atMs: 0, offsetMs: 0, durationMs: 60000, lines: [{ atMs: 0, text: "l0" }, { atMs: 1000, text: "l1" }] } as any;
const CUE = { type: "cue", cueId: "c1", title: "Sun", body: "far", atMs: 0 } as any;

const popups: Record<string, (t: any) => Promise<void>> = {
  translation: async (t) => { t.h().onFinal?.(FINAL); t.h().onTranslation?.(TR); await settle(); },
  song: async (t) => { t.h().onSong(SONG); await settle(); },
  cue: async (t) => { t.h().onCue(CUE); await settle(); },
  songOverTranslation: async (t) => { t.h().onFinal?.(FINAL); t.h().onTranslation?.(TR); t.h().onSong(SONG); await settle(); },
  menuOverTranslation: async (t) => { t.h().onFinal?.(FINAL); t.h().onTranslation?.(TR); await settle(); await t.doubleTap(); },
};
const stops: Record<string, (t: any) => Promise<void>> = {
  phone: (t) => t.phoneStop(),
  fatalError: async (t) => { t.h().onError({ type: "error", code: "internal", message: "x", fatal: true }); await settle(); },
  disable: async (t) => { t.controls.disable(); await settle(); },
};

describe("lens teardown when a session stops", () => {
  for (const [pk, open] of Object.entries(popups))
    for (const [sk, stop] of Object.entries(stops))
      it(`${pk} up, stop via ${sk}: back to the plain page, and the next session is clean`, async () => {
        const t = await boot();
        await open(t);
        expect(t.host.page).toBeGreaterThan(4);
        const n = t.rebuilds.length;
        await stop(t);
        await vi.advanceTimersByTimeAsync(1000);
        expect(t.rebuilds.length).toBe(n + 1);
        expect(t.last().containerTotalNum).toBe(4);
        expect(t.host.page).toBe(4);
        if (sk === "disable") {
          expect(t.text(C().caption)).toBe(ctl.SIGN_IN_PROMPT);
          expect(t.text(C().status)).toBe("not signed in");
        } else {
          expect(t.text(C().caption)).toBe(ctl.IDLE_PROMPT);
          expect(t.text(C().status)).toBe("ready");
        }
        // The ticker must not repaint a box after the stop.
        const menuBefore = t.text(C().menu);
        await vi.advanceTimersByTimeAsync(ctl.TICK_MS * 5);
        expect(t.text(C().menu)).toBe(menuBefore);
        expect(t.rebuilds.length).toBe(n + 1);
        if (sk === "disable") {
          t.controls.enable();
          await settle();
        }
        await t.phoneStart();
        await vi.advanceTimersByTimeAsync(1000);
        expect(t.rebuilds.length).toBe(n + 1); // no stray rebuild on start
        expect(t.host.page).toBe(4);
        // The new session's first translation still opens a box and closes on done.
        t.h().onFinal?.(FINAL);
        t.h().onTranslation?.(TR);
        await settle();
        expect(t.host.page).toBe(6);
        t.h().onTranslationDone?.({ type: "translation.done" });
        await settle();
        expect(t.host.page).toBe(4);
      });

  it("does not rebuild on a stop with no box up (every path)", async () => {
    for (const stop of Object.values(stops)) {
      const t = await boot();
      const n = t.rebuilds.length;
      await stop(t);
      expect(t.rebuilds.length).toBe(n);
    }
  });

  it("menu fallback mode: stop via the menu and via the phone", async () => {
    const t = await boot();
    t.ctlr.mode = "fail";
    await t.doubleTap();
    expect(t.text(C().caption)).toContain("Exit session");
    await t.swipeDown();
    await t.click();
    expect(t.stops.length).toBe(1);
    expect(t.text(C().caption)).toBe(ctl.IDLE_PROMPT);
    t.ctlr.mode = "ok";
    await t.phoneStart();
    expect(t.host.page).toBe(4);
    t.ctlr.mode = "fail";
    await t.doubleTap();
    await t.phoneStop();
    expect(t.text(C().caption)).toBe(ctl.IDLE_PROMPT);
    expect(t.text(C().status)).toBe("ready");
  });

  for (const pk of ["translation", "song", "cue"])
    it(`a ${pk} box whose rebuild timed out but landed late is torn down on stop`, async () => {
      const t = await boot();
      t.ctlr.mode = "late";
      await popups[pk](t);
      // withBleTimeout gives up at 4s (box marked dropped); the host applies it at 6s.
      await vi.advanceTimersByTimeAsync(7000);
      expect(t.host.page).toBeGreaterThan(4);
      t.ctlr.mode = "ok";
      await t.phoneStop();
      await vi.advanceTimersByTimeAsync(1000);
      expect(t.host.page).toBe(4);
      expect(t.text(C().caption)).toBe(ctl.IDLE_PROMPT);
      expect(t.text(C().status)).toBe("ready");
    });

  it("retries a failed teardown when the next session starts", async () => {
    const t = await boot();
    await popups.translation(t);
    t.ctlr.mode = "fail";
    await t.phoneStop();
    expect(t.host.page).toBe(6); // the teardown rebuild failed
    t.ctlr.mode = "ok";
    await t.phoneStart();
    await vi.advanceTimersByTimeAsync(1000);
    expect(t.host.page).toBe(4);
    // Once healed, a later start does not rebuild again.
    await t.phoneStop();
    const n = t.rebuilds.length;
    await t.phoneStart();
    expect(t.rebuilds.length).toBe(n);
  });

  it("retries a failed teardown on the next stop too", async () => {
    const t = await boot();
    await popups.translation(t);
    t.ctlr.mode = "fail";
    await t.phoneStop();
    await t.phoneStart(); // the start's retry fails as well
    t.ctlr.mode = "ok";
    await t.phoneStop();
    expect(t.host.page).toBe(4);
  });

  it("a failed teardown is retried on sign-out, not left over the sign-in prompt", async () => {
    const t = await boot();
    await popups.translation(t);
    t.ctlr.mode = "fail";
    await t.phoneStop();
    expect(t.host.page).toBe(6);
    t.ctlr.mode = "ok";
    t.controls.disable();
    await settle();
    expect(t.host.page).toBe(4);
    expect(t.text(C().caption)).toBe(ctl.SIGN_IN_PROMPT);
    expect(t.text(C().status)).toBe("not signed in");
  });

  it("sign-out mid-session with a stale page rebuilds once", async () => {
    const t = await boot();
    await popups.translation(t);
    t.ctlr.mode = "fail";
    await t.phoneStop();
    t.ctlr.mode = "ok";
    await t.phoneStart(); // retry rebuild clears the stale page
    t.ctlr.mode = "fail";
    await popups.translation(t); // box rebuild fails -> stale again
    t.ctlr.mode = "ok";
    const n = t.rebuilds.length;
    t.controls.disable();
    await settle();
    expect(t.rebuilds.length).toBe(n + 1);
    expect(t.host.page).toBe(4);
  });

  it("ignores late callbacks from a stopped client", async () => {
    const t = await boot();
    const old = t.h();
    await t.phoneStop();
    const n = t.rebuilds.length;
    old.onFinal?.(FINAL);
    old.onTranslation?.(TR);
    old.onSong?.(SONG);
    old.onCue?.(CUE);
    old.onError?.({ type: "error", code: "unauthorized", message: "x" } as any);
    await settle();
    expect(t.rebuilds.length).toBe(n);
    expect(t.host.page).toBe(4);
    expect(t.text(C().caption)).toBe(ctl.IDLE_PROMPT);
    expect(t.text(C().status)).toBe("ready"); // still signed in
  });
});
