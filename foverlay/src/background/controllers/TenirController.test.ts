/**
 * TenirController behaviour tests — the valuable pure behaviours extracted
 * from upstream `even/tests/controller.test.ts` (which drove a stub Even
 * bridge), re-based on a fake MiniappSession + fake ApiClient:
 * boot auth, tap start/stop, mic subscribe/teardown discipline, segment cap,
 * caption-band derivation, snapshot resume, silent re-login, and the UI RPCs.
 */

import { beforeEach, describe, expect, it } from "bun:test";

import { bytesToBase64 } from "@mentra/miniapp/background";
import type { MiniappSession } from "@mentra/miniapp/background";

import type { ApiHandlers, SessionParams } from "../../core/ws";
import type { Channels } from "../../shared/channels";
import { IDLE_PROMPT, SIGN_IN_PROMPT } from "../hud";
import {
  CREDENTIALS_KEY,
  SERVER_URL_KEY,
  SESSION_KEY,
  TOKEN_KEY,
  TenirController,
  type CaptureClient,
} from "./TenirController";

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

class FakeClient implements CaptureClient {
  started: Array<{ params: SessionParams; resume?: string }> = [];
  stopped = 0;
  audio: Uint8Array[] = [];

  constructor(
    readonly url: string,
    readonly handlers: ApiHandlers,
  ) {}

  start(params: SessionParams, resumeSessionId?: string): void {
    this.started.push({ params, resume: resumeSessionId });
  }

  stop(): void {
    this.stopped += 1;
  }

  sendAudio(pcm: Uint8Array): boolean {
    this.audio.push(pcm);
    return true;
  }
}

interface FakeWorld {
  session: MiniappSession;
  storage: Map<string, string>;
  clients: FakeClient[];
  micActive: () => number;
  emitAudio: (bytes: Uint8Array) => void;
  emitTouch: (kind: string) => void;
  rpc: <C extends keyof Channels & string>(channel: C, payload: unknown) => Promise<unknown>;
  uiSent: Array<{ channel: string; payload: unknown }>;
  rendered: () => Array<{ id?: string; text?: string }>;
  openUi: () => void;
}

function makeWorld(seed: Record<string, string> = {}): FakeWorld {
  const storage = new Map<string, string>(Object.entries(seed));
  const micHandlers = new Set<(d: { data: string }) => void>();
  let touchHandler: ((d: { kind: string }) => void) | null = null;
  const rpcHandlers = new Map<string, (payload: never) => unknown>();
  const openHandlers: Array<() => void> = [];
  const broadcastHandlers = new Map<string, (payload: never) => void>();
  const uiSent: Array<{ channel: string; payload: unknown }> = [];
  let lastScene: Array<{ id?: string; text?: string }> = [];

  const session = {
    storage: {
      get: async (key: string) => storage.get(key) ?? null,
      set: async (key: string, value: string) => void storage.set(key, value),
      delete: async (key: string) => void storage.delete(key),
    },
    ui: {
      send: (channel: string, payload: unknown) => uiSent.push({ channel, payload }),
      on: (channel: string, cb: (payload: never) => void) => {
        broadcastHandlers.set(channel, cb);
        return () => broadcastHandlers.delete(channel);
      },
      handle: (channel: string, handler: (payload: never) => unknown) => {
        rpcHandlers.set(channel, handler);
        return () => rpcHandlers.delete(channel);
      },
      onOpen: (cb: () => void) => {
        openHandlers.push(cb);
        return () => {};
      },
    },
    mic: {
      onAudioChunk: (handler: (d: { data: string }) => void) => {
        micHandlers.add(handler);
        return () => micHandlers.delete(handler);
      },
    },
    input: {
      onTouch: (handler: (d: { kind: string }) => void) => {
        touchHandler = handler;
        return () => {
          touchHandler = null;
        };
      },
    },
    display: {
      render: (elements: Array<{ id?: string; text?: string }>) => {
        lastScene = elements;
        return Promise.resolve({ status: "displayed" });
      },
    },
    system: {
      openUrl: () => {},
    },
    onVisibilityChange: () => () => {},
    onBeforeDisconnect: () => () => {},
    on: () => () => {},
  };

  return {
    session: session as unknown as MiniappSession,
    storage,
    clients: [],
    micActive: () => micHandlers.size,
    emitAudio: (bytes) => {
      for (const h of [...micHandlers]) h({ data: bytesToBase64(bytes) });
    },
    emitTouch: (kind) => touchHandler?.({ kind }),
    rpc: async (channel, payload) => {
      const handler = rpcHandlers.get(channel);
      if (!handler) throw new Error(`no handler for ${channel}`);
      return await handler(payload as never);
    },
    uiSent,
    rendered: () => lastScene,
    openUi: () => {
      for (const cb of openHandlers) cb();
    },
  };
}

// ---- fetch stub ------------------------------------------------------------

type Route = (init?: RequestInit) => {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
};

const realFetch = globalThis.fetch;
let routes: Record<string, Route>;
let fetchCalls: Array<{ url: string; init?: RequestInit }>;

function stubFetch(): void {
  globalThis.fetch = (async (url: string | URL, init?: RequestInit) => {
    const u = String(url);
    fetchCalls.push({ url: u, init });
    const key = Object.keys(routes).find((k) => u.includes(k));
    if (!key) throw new TypeError(`unroutable fetch: ${u}`);
    const out = routes[key](init);
    const headers = new Map(Object.entries(out.headers ?? {}));
    return {
      ok: out.status >= 200 && out.status < 300,
      status: out.status,
      statusText: String(out.status),
      headers: { get: (name: string) => headers.get(name.toLowerCase()) ?? null },
      json: async () => out.body ?? {},
    } as unknown as Response;
  }) as unknown as typeof fetch;
}

const PRINCIPAL = { userId: "u1", username: "ada", household: "h", role: "member" };

const AUTHED_SEED = {
  [SERVER_URL_KEY]: "wss://h.example.com/ws",
  [TOKEN_KEY]: "tok-1",
  [CREDENTIALS_KEY]: JSON.stringify({ username: "ada", password: "pw" }),
};

function makeController(world: FakeWorld): TenirController {
  return new TenirController(world.session, {
    createClient: (url, handlers) => {
      const client = new FakeClient(url, handlers);
      world.clients.push(client);
      return client;
    },
    now: () => new Date(2026, 0, 1, 9, 30),
  });
}

const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  routes = {};
  fetchCalls = [];
  stubFetch();
});

// Restore the real fetch when the file's suites are done (bun runs files in
// isolated workers, so a simple process-level restore is enough).
process.on("beforeExit", () => {
  globalThis.fetch = realFetch;
});

// ---------------------------------------------------------------------------
// Suites
// ---------------------------------------------------------------------------

describe("boot", () => {
  it("boots signed out with no configured server and shows the sign-in prompt", async () => {
    const world = makeWorld();
    const c = makeController(world);
    await c.start();
    expect(c.authState().signedIn).toBe(false);
    expect(c.hudFrame()).toEqual({ status: "not signed in", clock: "", caption: SIGN_IN_PROMPT });
    // Taps do nothing while signed out.
    world.emitTouch("single_tap");
    expect(world.clients).toHaveLength(0);
    expect(fetchCalls).toHaveLength(0);
    c.stop();
  });

  it("boots signed in from a stored token and idles at tap-to-start", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    expect(c.authState()).toEqual({
      signedIn: true,
      username: "ada",
      serverUrl: "h.example.com",
    });
    expect(c.hudFrame()).toEqual({ status: "ready", clock: "9:30 AM", caption: IDLE_PROMPT });
    // The idle frame reached the display with stable element ids.
    expect(world.rendered().map((e) => e.id)).toEqual(["status", "clock", "caption"]);
    c.stop();
  });

  it("falls back to a silent re-login when the stored token is rejected", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    let meCalls = 0;
    routes["/auth/me"] = () => (++meCalls === 1 ? { status: 401 } : { status: 200, body: PRINCIPAL });
    routes["/auth/login"] = () => ({ status: 200, body: { token: "tok-2" } });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    await flush();
    expect(c.authState().signedIn).toBe(true);
    expect(world.storage.get(TOKEN_KEY)).toBe("tok-2"); // fresh token persisted
    c.stop();
  });

  it("stays signed in best-effort when the server is unreachable but a session is cached", async () => {
    routes["/auth/me"] = () => {
      throw new TypeError("network down");
    };
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    expect(c.authState().signedIn).toBe(true);
    expect(c.authState().username).toBe("ada");
    c.stop();
  });
});

describe("session flow", () => {
  async function signedIn(seed: Record<string, string> = AUTHED_SEED) {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(seed);
    const c = makeController(world);
    await c.start();
    return { world, c };
  }

  it("single tap starts a session, streams mic PCM, and a second tap stops it", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    expect(world.clients).toHaveLength(1);
    const client = world.clients[0];
    expect(client.url).toBe("wss://h.example.com/ws");
    expect(client.started[0].params.micSource).toBe("g2-microphone");
    expect(client.started[0].resume).toBeUndefined();
    expect(world.micActive()).toBe(1);

    client.handlers.onConnectionChange?.("open");
    expect(c.hudFrame().status).toBe("listening.");

    const pcm = new Uint8Array([1, 2, 3, 4]);
    world.emitAudio(pcm);
    expect(client.audio).toHaveLength(1);
    expect([...client.audio[0]]).toEqual([1, 2, 3, 4]);

    world.emitTouch("single_tap"); // toggle off
    expect(client.stopped).toBe(1);
    expect(world.micActive()).toBe(0); // NEVER left subscribed after stop
    expect(c.liveState().recording).toBe(false);
    expect(c.hudFrame().caption).toBe(IDLE_PROMPT);

    // A chunk arriving after stop is dropped, not sent.
    world.emitAudio(pcm);
    expect(client.audio).toHaveLength(1);
    c.stop();
  });

  it("partials and finals build the caption band; finals cap at 60 and shift cue anchors", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    h.onConnectionChange?.("open");

    h.onPartial?.({ type: "caption.partial", text: "hel" });
    expect(c.hudFrame().caption.endsWith("hel")).toBe(true);

    h.onFinal?.({ type: "caption.final", segmentId: "s0", text: "hello world", startMs: 0, endMs: 1 });
    expect(c.liveState().partial).toBe("");
    expect(c.hudFrame().caption.endsWith("hello world")).toBe(true);

    h.onCue?.({ type: "cue", cueId: "q1", title: "T", body: "B", atMs: 0 });
    expect(c.liveState().cues[0].afterIndex).toBe(0); // anchored after s0

    for (let i = 1; i <= 65; i++) {
      h.onFinal?.({ type: "caption.final", segmentId: `s${i}`, text: `turn ${i}`, startMs: i, endMs: i + 1 });
    }
    const live = c.liveState();
    expect(live.segments).toHaveLength(60);
    expect(live.segments[0].id).toBe("s6"); // oldest turns fell off
    expect(live.cues[0].afterIndex).toBe(-6); // anchor shifted with the window
    c.stop();
  });

  it("pairs a translation with its turn and mirrors cue/song state to the UI", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    h.onFinal?.({ type: "caption.final", segmentId: "s1", text: "hola", lang: "es", startMs: 0, endMs: 1 });
    h.onTranslation?.({ type: "translation", segmentId: "s1", text: "hello", sourceLang: "es" });
    expect(c.liveState().segments[0].translation).toBe("hello");

    h.onSong?.({
      type: "song",
      songId: "sng",
      title: "Weird Fishes",
      artist: "Radiohead",
      atMs: 0,
      offsetMs: 0,
      lines: [],
    });
    expect(c.liveState().song).toEqual({ id: "sng", title: "Weird Fishes", artist: "Radiohead" });
    h.onSongDone?.({ type: "song.done", songId: "sng" });
    expect(c.liveState().song).toBeNull();
    c.stop();
  });

  it("double tap clears the caption band but keeps the session recording", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    h.onFinal?.({ type: "caption.final", segmentId: "s1", text: "hello", startMs: 0, endMs: 1 });
    world.emitTouch("double_tap");
    const live = c.liveState();
    expect(live.segments).toHaveLength(0);
    expect(live.recording).toBe(true);
    expect(world.clients[0].stopped).toBe(0);
    expect(world.micActive()).toBe(1);
    c.stop();
  });

  it("ignores other gestures", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("swipe_up");
    world.emitTouch("long_press");
    expect(world.clients).toHaveLength(0);
    c.stop();
  });

  it("resumes a persisted snapshot on start and clears it on a clean stop", async () => {
    const snapshot = { sessionId: "sess-9", micSource: "g2-microphone", transcript: "earlier text" };
    const { world, c } = await signedIn({
      ...AUTHED_SEED,
      [SESSION_KEY]: JSON.stringify(snapshot),
    });
    // The snapshot means the JSContext died mid-session — resume it.
    expect(world.clients).toHaveLength(1);
    expect(world.clients[0].started[0].resume).toBe("sess-9");
    const live = c.liveState();
    expect(live.recording).toBe(true);
    expect(live.segments).toEqual([{ id: "restored", text: "earlier text" }]);

    world.emitTouch("single_tap"); // clean stop
    await flush();
    expect(world.storage.has(SESSION_KEY)).toBe(false); // nothing to resume anymore
    c.stop();
  });

  it("heals an unauthorized rejection with one silent re-login, then reconnects", async () => {
    routes["/auth/login"] = () => ({ status: 200, body: { token: "tok-2" } });
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    world.clients[0].handlers.onError?.({
      type: "error",
      code: "unauthorized",
      message: "rejected",
      fatal: true,
    });
    await flush();
    expect(world.clients).toHaveLength(2); // reconnected with the fresh token
    expect(world.clients[0].stopped).toBe(1);
    expect(c.liveState().recording).toBe(true);
    expect(world.micActive()).toBe(1); // exactly one live subscription
    c.stop();
  });

  it("disables when the silent re-login is rejected too", async () => {
    routes["/auth/login"] = () => ({ status: 401, body: { detail: "bad creds" } });
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    world.clients[0].handlers.onError?.({
      type: "error",
      code: "unauthorized",
      message: "rejected",
      fatal: true,
    });
    await flush();
    expect(c.authState().signedIn).toBe(false);
    expect(c.liveState().recording).toBe(false);
    expect(world.micActive()).toBe(0);
    expect(c.hudFrame().caption).toBe(SIGN_IN_PROMPT);
    c.stop();
  });
});

describe("UI bus", () => {
  it("sends a full snapshot on WebView open", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    world.uiSent.length = 0;
    world.openUi();
    expect(world.uiSent).toHaveLength(1);
    expect(world.uiSent[0].channel).toBe("tenir:snapshot");
    expect(world.uiSent[0].payload).toMatchObject({
      auth: { signedIn: true, username: "ada" },
      live: { recording: false, connection: "closed" },
    });
    c.stop();
  });

  it("tenir:login normalizes the URL, persists everything, and maps error cases", async () => {
    const world = makeWorld();
    const c = makeController(world);
    await c.start();

    // Bad URL → upstream's validation message; no network traffic.
    expect(await world.rpc("tenir:login", { serverUrl: "   ", username: "a", password: "b" })).toEqual({
      ok: false,
      error: "Enter your server address, e.g. tenir.example.com",
    });

    // 401 → upstream's friendly message.
    routes["/auth/login"] = () => ({ status: 401, body: { detail: "nope" } });
    expect(
      await world.rpc("tenir:login", { serverUrl: "tenir.example.com", username: "ada", password: "x" }),
    ).toEqual({ ok: false, error: "Incorrect username or password." });

    // Network failure → upstream's reachability message.
    routes["/auth/login"] = () => {
      throw new TypeError("down");
    };
    expect(
      await world.rpc("tenir:login", { serverUrl: "tenir.example.com", username: "ada", password: "x" }),
    ).toEqual({
      ok: false,
      error: "Can't reach the server — check it's running and the server URL is correct.",
    });

    // 5xx → server-error message.
    routes["/auth/login"] = () => ({ status: 500, body: { detail: "boom" } });
    expect(
      await world.rpc("tenir:login", { serverUrl: "tenir.example.com", username: "ada", password: "x" }),
    ).toEqual({ ok: false, error: "Server error (500): boom" });

    // Success → token + credentials + normalized URL persisted, auth flips.
    routes["/auth/login"] = () => ({ status: 200, body: { token: "tok-9" } });
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    expect(
      await world.rpc("tenir:login", { serverUrl: "tenir.example.com", username: "ada", password: "pw" }),
    ).toEqual({ ok: true, username: "ada" });
    await flush();
    expect(world.storage.get(SERVER_URL_KEY)).toBe("wss://tenir.example.com/ws");
    expect(world.storage.get(TOKEN_KEY)).toBe("tok-9");
    expect(JSON.parse(world.storage.get(CREDENTIALS_KEY)!)).toEqual({ username: "ada", password: "pw" });
    expect(c.authState().signedIn).toBe(true);
    // The login POST hit the https base derived from the ws URL.
    expect(fetchCalls.some((f) => f.url === "https://tenir.example.com/auth/login")).toBe(true);
    c.stop();
  });

  it("tenir:logout clears the token + credentials and stops a running session", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    world.emitTouch("single_tap");
    expect(await world.rpc("tenir:logout", {})).toEqual({ ok: true });
    await flush();
    expect(c.authState().signedIn).toBe(false);
    expect(world.storage.has(TOKEN_KEY)).toBe(false);
    expect(world.storage.has(CREDENTIALS_KEY)).toBe(false);
    expect(world.clients[0].stopped).toBe(1);
    expect(world.micActive()).toBe(0);
    c.stop();
  });

  it("tenir:start / tenir:stop drive the same transitions a tap does", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    expect(await world.rpc("tenir:start", {})).toEqual({ ok: true });
    expect(c.liveState().recording).toBe(true);
    expect(await world.rpc("tenir:start", {})).toEqual({ ok: true }); // idempotent
    expect(world.clients).toHaveLength(1);
    expect(await world.rpc("tenir:stop", {})).toEqual({ ok: true });
    expect(c.liveState().recording).toBe(false);
    c.stop();
  });

  it("tenir:fetch proxies REST with auth and adopts a renewed token", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    routes["/conversations"] = (init) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      expect(headers.Authorization).toBe("Bearer tok-1");
      return {
        status: 200,
        body: [{ id: "c1" }],
        headers: { "x-renewed-token": "tok-fresh" },
      };
    };
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    expect(await world.rpc("tenir:fetch", { path: "/conversations?limit=50&offset=0" })).toEqual({
      ok: true,
      data: [{ id: "c1" }],
    });
    await flush();
    expect(world.storage.get(TOKEN_KEY)).toBe("tok-fresh"); // sliding renewal persisted

    routes["/conversations"] = () => ({ status: 404, body: { detail: "gone" } });
    expect(await world.rpc("tenir:fetch", { path: "/conversations/nope" })).toEqual({
      ok: false,
      error: "404: gone",
      status: 404,
    });
    c.stop();
  });

  it("tenir:fetch refuses while signed out", async () => {
    const world = makeWorld();
    const c = makeController(world);
    await c.start();
    expect(await world.rpc("tenir:fetch", { path: "/conversations" })).toEqual({
      ok: false,
      error: "Not signed in.",
    });
    c.stop();
  });
});
