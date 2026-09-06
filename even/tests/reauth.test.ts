/**
 * The unauthorized/error handling on the lens controller (XERK-236).
 *
 * Two defects the first full QA pass reproduced against a real api:
 *
 * 1. An unbounded, backoff-free re-login storm. `connect()` reset the one-shot
 *    `reauthAttempted` guard, and the unauthorized handler calls `connect()`
 *    after a silent re-login — so the guard was re-armed every time round the
 *    loop. A server that keeps rejecting the token got 167 logins + 167 socket
 *    upgrades in 15 s against a real api (~5000 in 10 s with no network
 *    latency), while the lens sat on "connecting to server…" forever.
 *
 * 2. Every error frame that was not `unauthorized` was dropped. The api sends
 *    `error{internal}` when a session fails to start (e.g. the STT backend is
 *    down) and never follows it with `session.ready` — so the lens read
 *    "listening…", the mic stayed subscribed, and PCM streamed into a socket
 *    with no session. Nothing recorded, and nothing said so.
 */

import { OsEventTypeList, type EvenAppBridge, type EvenHubEvent } from "@evenrealities/even_hub_sdk";
import type { ApiHandlers, SessionParams } from "@tenir/client-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemStorage } from "./memStorage";

// The silent re-login is the thing being counted, so it is stubbed rather than
// driven through a real `login()`.
const silentLogin = vi.fn();
vi.mock("../src/state/credentials", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/state/credentials")>()),
  silentLogin: (...args: unknown[]) => silentLogin(...args),
}));

let controllerMod: typeof import("../src/lens/controller");
let layout: typeof import("../src/lens/layout");
let cfg: typeof import("../src/config");

beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 6, 22, 14, 5));
  vi.resetModules();
  silentLogin.mockReset();
  controllerMod = await import("../src/lens/controller");
  layout = await import("../src/lens/layout");
  cfg = await import("../src/config");
  await cfg.initConfig(new MemStorage());
});

afterEach(() => {
  vi.useRealTimers();
  // The OIDC sidecar store write-throughs to localStorage; clear it so an OIDC
  // session never leaks into a later (built-in) test.
  try {
    localStorage.clear();
  } catch {
    /* no localStorage in this env */
  }
});

const settle = async () => {
  await vi.advanceTimersByTimeAsync(0);
  await Promise.resolve();
  await vi.advanceTimersByTimeAsync(0);
};

function harness() {
  let handler: ((e: EvenHubEvent) => void) | undefined;
  const store = new Map<string, string>();
  const bridge = {
    onEvenHubEvent: (h: (e: EvenHubEvent) => void) => {
      handler = h;
      return () => {
        handler = undefined;
      };
    },
    // The capture path calls this on every connect; omitting it surfaces as an
    // unhandled rejection that fails the RUN while every test still "passes".
    audioControl: async () => true,
    getLocalStorage: async (k: string) => store.get(k) ?? "",
    setLocalStorage: async (k: string, v: string) => {
      store.set(k, v);
      return true;
    },
    shutDownPageContainer: async () => true,
    shutdownApp: async () => true,
    rebuildPageContainer: async () => true,
  } as unknown as EvenAppBridge;

  // Every client the controller creates, so a reconnect is visible as a new one.
  const clients: Array<{ handlers: ApiHandlers; started: number; stopped: number }> = [];
  const createClient = (_url: string, h: ApiHandlers) => {
    const rec = { handlers: h, started: 0, stopped: 0 };
    clients.push(rec);
    return {
      start: (_p: SessionParams, _resume?: string) => {
        rec.started += 1;
      },
      stop: () => {
        rec.stopped += 1;
      },
      sendAudio: () => true,
    };
  };

  const latest = new Map<number, string>();
  const writer = new layout.LensTextWriter(async (c, content) => {
    latest.set(c.id, content);
    return true;
  });

  return {
    bridge,
    clients,
    createClient,
    writer,
    text: (c: { id: number }) => latest.get(c.id),
    emit: (e: EvenHubEvent) => handler?.(e),
  };
}

async function boot() {
  const h = harness();
  const controls = await controllerMod.wireLens(h.bridge, new MemStorage(), h.writer, null, {
    createClient: h.createClient,
  });
  await settle();
  controls.enable();
  await settle();
  // A temple tap starts a session (XERK-85).
  h.emit({ sysEvent: { eventType: OsEventTypeList.CLICK_EVENT } } as EvenHubEvent);
  await vi.advanceTimersByTimeAsync(controllerMod.GESTURE_DEDUPE_MS + 50);
  return { ...h, controls };
}

const unauthorized = () =>
  ({
    type: "error" as const,
    code: "unauthorized" as const,
    message: "connection rejected — please sign in again",
    fatal: true,
  });

describe("lens controller: repeated unauthorized rejections (XERK-236)", () => {
  it("re-logs in ONCE, however many times the server rejects the token", async () => {
    // A server that keeps saying no: every silent re-login "succeeds", so the
    // controller reconnects and is rejected again.
    silentLogin.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "member" });
    const t = await boot();
    expect(t.clients).toHaveLength(1);

    for (let i = 0; i < 20; i += 1) {
      t.clients[t.clients.length - 1].handlers.onError?.(unauthorized());
      await settle();
    }

    // Before the fix this was 20 logins and 20 fresh clients, as fast as the
    // event loop allowed. The guard is only cleared by a session that actually
    // starts (onReady), so one failed round is all a rejection loop gets.
    expect(silentLogin).toHaveBeenCalledTimes(1);
    expect(t.clients.length).toBeLessThanOrEqual(2);
  });

  it("a successful session re-arms the single retry for a LATER expiry", async () => {
    silentLogin.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "member" });
    const t = await boot();

    t.clients[0].handlers.onError?.(unauthorized());
    await settle();
    expect(silentLogin).toHaveBeenCalledTimes(1);

    // The reconnect works this time: the session comes up.
    const live = t.clients[t.clients.length - 1];
    live.handlers.onReady?.({ type: "session.ready", sessionId: "s-1", resumed: false });
    await settle();

    // Much later, the new token expires too — that must get its own retry.
    live.handlers.onError?.(unauthorized());
    await settle();
    expect(silentLogin).toHaveBeenCalledTimes(2);
  });
});

describe("lens controller: OIDC session reauth (XERK-656)", () => {
  it("does NOT replay cached credentials on unauthorized — OIDC re-auth is the phone", async () => {
    // An active OIDC session (sidecar present): the socket already tried an IdP
    // refresh before surfacing this, so there is nothing for the glasses to do
    // but send the wearer back to the phone to sign in with Authentik again.
    const core = await import("@tenir/client-core");
    core.setOidcSession({ refreshToken: "r", expiresAt: Date.now() + 60_000 });
    expect(core.getSessionKind()).toBe("oidc");

    const t = await boot();
    t.clients[0].handlers.onError?.(unauthorized());
    await settle();

    // No credential re-login, and no reconnect client spun up.
    expect(silentLogin).not.toHaveBeenCalled();
    expect(t.clients).toHaveLength(1);
  });
});

describe("lens controller: non-auth error frames (XERK-236)", () => {
  it("stops the capture when the session never started", async () => {
    const t = await boot();
    // What the api really sends when Session()/start() raises — e.g. the STT
    // backend is unreachable. `session.ready` never follows.
    t.clients[0].handlers.onError?.({
      type: "error",
      code: "internal",
      message: "could not start session",
      fatal: false,
    });
    await settle();

    expect(t.clients[0].stopped).toBeGreaterThanOrEqual(1);
    // The lens must not still claim to be listening.
    expect(t.text(layout.CONTAINER.status) ?? "").not.toContain("listening");
    expect(silentLogin).not.toHaveBeenCalled();
  });

  it("leaves a healthy running session alone on a transient error", async () => {
    const t = await boot();
    t.clients[0].handlers.onReady?.({ type: "session.ready", sessionId: "s-1", resumed: false });
    await settle();

    t.clients[0].handlers.onError?.({
      type: "error",
      code: "bad_request",
      message: "could not parse message",
      fatal: false,
    });
    await settle();

    // A stray bad frame mid-conversation must not end the recording.
    expect(t.clients[0].stopped).toBe(0);
  });
});
