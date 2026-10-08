import { OsEventTypeList, type EvenAppBridge, type EvenHubEvent } from "@evenrealities/even_hub_sdk";
import type { ApiHandlers } from "@tenir/client-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemStorage } from "./memStorage";

/**
 * A silent re-login (after an unauthorized error) resolves asynchronously. If the
 * wearer stopped — or started another session — meanwhile, its result must not
 * reconnect a ghost session (mic on, nothing to stop it) nor sign the wearer out.
 */
const silentLogin = vi.fn();
vi.mock("../src/state/credentials", async (orig) => ({
  ...(await orig<typeof import("../src/state/credentials")>()),
  silentLogin: (...a: unknown[]) => silentLogin(...a),
}));

let ctl: typeof import("../src/lens/controller");
let layout: typeof import("../src/lens/layout");
let sessionMod: typeof import("../src/phone/session");
let cfg: typeof import("../src/config");
beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 6, 22, 14, 5));
  vi.resetModules();
  silentLogin.mockReset();
  ctl = await import("../src/lens/controller");
  layout = await import("../src/lens/layout");
  sessionMod = await import("../src/phone/session");
  cfg = await import("../src/config");
  await cfg.initConfig(new MemStorage());
});
afterEach(() => { vi.useRealTimers(); document.body.innerHTML = ""; });
const settle = async () => { await vi.advanceTimersByTimeAsync(0); await Promise.resolve(); await vi.advanceTimersByTimeAsync(0); };
type Page = { containerTotalNum?: number };
type Rec = { h: ApiHandlers; started: number; stopped: number; resume?: string };

async function boot(opts: { snapshot?: string; start?: boolean } = {}) {
  let handler: ((e: EvenHubEvent) => void) | null = null;
  const rebuilds: Page[] = [];
  const host = { page: 4 };
  const ctlr = { mode: "ok" as "ok" | "fail" | "late" };
  const kv = new Map<string, string>();
  if (opts.snapshot) kv.set("tenir.session", opts.snapshot);
  const mic: boolean[] = [];
  const bridge = {
    onEvenHubEvent: (h: any) => { handler = h; return () => { handler = null; }; },
    audioControl: async (on: boolean) => { mic.push(on); return true; },
    getLocalStorage: async (k: string) => kv.get(k) ?? "",
    setLocalStorage: async (k: string, v: string) => { kv.set(k, v); return true; },
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
  const clients: Rec[] = [];
  const createClient = (_u: string, h: ApiHandlers) => {
    const rec: Rec = { h, started: 0, stopped: 0 };
    clients.push(rec);
    return { start: (_p: unknown, r?: string) => { rec.started++; rec.resume = r; }, stop: () => { rec.stopped++; }, sendAudio: () => true };
  };
  const controls = await ctl.wireLens(bridge, new MemStorage(), writer, phone, { createClient } as any);
  await settle();
  const sys = async (eventType: OsEventTypeList) => { handler?.({ sysEvent: { eventType } } as EvenHubEvent); await vi.advanceTimersByTimeAsync(ctl.GESTURE_DEDUPE_MS + 50); };
  const t = {
    controls, rebuilds, host, ctlr, clients, mic, kv,
    h: () => clients[clients.length - 1].h,
    text: (c: { id: number }) => latest.get(c.id),
    click: () => sys(OsEventTypeList.CLICK_EVENT),
    doubleTap: () => sys(OsEventTypeList.DOUBLE_CLICK_EVENT),
    phoneStop: async () => { (document.getElementById("session-stop") as HTMLButtonElement).click(); await settle(); },
    phoneStart: async () => { (document.getElementById("session-start") as HTMLButtonElement).click(); await settle(); },
    badge: () => document.getElementById("session-badge")!.textContent,
  };
  controls.enable(); await settle();
  if (opts.start !== false && !opts.snapshot) { await t.click(); await settle(); }
  if (t.clients.length) { t.h().onReady?.({ type: "session.ready", sessionId: "sess1" } as any); await settle(); }
  return t;
}
const C = () => layout.CONTAINER;
const UNAUTH = { type: "error", code: "unauthorized", message: "x", fatal: true } as any;
const USER = { userId: "u", username: "ada", household: "lab", role: "member" };
function deferred<T>() { let res!: (v: T) => void; const p = new Promise<T>((r) => { res = r; }); return { p, res }; }

describe("silent re-login racing a stop", () => {
  it("a heal that succeeds after the stop does not reconnect a ghost session", async () => {
    const d = deferred<any>();
    silentLogin.mockReturnValue(d.p);
    const t = await boot();
    t.clients[0].h.onError?.(UNAUTH);
    await settle();
    await t.phoneStop();
    const micCalls = t.mic.length;
    d.res(USER);
    await settle();
    expect(t.clients.length).toBe(1);
    expect(t.mic.slice(micCalls)).not.toContain(true); // the mic stays off
    expect(t.text(C().caption)).toBe(ctl.IDLE_PROMPT);
    expect(t.text(C().status)).toBe("ready");
  });

  it("a heal that fails after the stop does not sign the wearer out", async () => {
    const d = deferred<any>();
    silentLogin.mockReturnValue(d.p);
    const t = await boot();
    t.clients[0].h.onError?.(UNAUTH);
    await settle();
    await t.phoneStop();
    d.res(null);
    await settle();
    expect(t.text(C().status)).toBe("ready");
    expect(t.text(C().caption)).toBe(ctl.IDLE_PROMPT);
  });

  it("a stale heal does not replace the client of a session started after the stop", async () => {
    const d = deferred<any>();
    silentLogin.mockReturnValue(d.p);
    const t = await boot();
    t.clients[0].h.onError?.(UNAUTH);
    await settle();
    await t.phoneStop();
    await t.phoneStart();
    expect(t.clients.length).toBe(2);
    d.res(USER);
    await settle();
    expect(t.clients.length).toBe(2);
    expect(t.clients[1].stopped).toBe(0);
  });

  it("a new session's first auth failure gets its own re-login, not a sign-out", async () => {
    const d = deferred<any>();
    silentLogin.mockReturnValue(d.p);
    const t = await boot();
    t.clients[0].h.onError?.(UNAUTH);
    await settle();
    await t.phoneStop();
    await t.phoneStart();
    t.clients[1].h.onError?.(UNAUTH); // before the first heal resolved
    await settle();
    expect(silentLogin).toHaveBeenCalledTimes(2);
    expect(t.text(C().status)).not.toBe("not signed in");
    d.res(USER);
    await settle();
    expect(t.clients.length).toBe(3); // the live session reconnected once
    expect(t.clients[1].stopped).toBe(1);
    expect(t.text(C().status)).not.toBe("not signed in");
  });

  it("a heal for the live client still reconnects it", async () => {
    silentLogin.mockResolvedValue(USER);
    const t = await boot();
    t.clients[0].h.onError?.(UNAUTH);
    await settle();
    expect(t.clients.length).toBe(2);
    expect(t.clients[0].stopped).toBe(1);
    expect(t.clients[1].started).toBe(1);
    expect(t.clients[1].resume).toBe("sess1"); // the same conversation resumes
  });
});
