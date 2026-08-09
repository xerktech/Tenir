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
  type TenirDeps,
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
  renderCount: () => number;
  /** Every scene accepted by the display, in order (blocked renders excluded). */
  allRenders: () => Array<Array<{ id?: string; text?: string }>>;
  /** Make subsequent display.render calls report this status ("blocked" = frame never shown). */
  setRenderStatus: (status: "displayed" | "blocked") => void;
  emitVisibility: (v: "foreground" | "background") => void;
  /** The host flipped between light and dark (XERK-237). */
  emitColorScheme: (s: "light" | "dark") => void;
  /** Clips handed to the host's download sheet. */
  downloads: Array<{ url: string; filename?: string; mimeType?: string }>;
  /** Stand in a specific host reply shape ({success} device / {ok} simulator). */
  setDownloadReply: (reply: Record<string, unknown>) => void;
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
  const acceptedRenders: Array<Array<{ id?: string; text?: string }>> = [];
  let renderStatus: "displayed" | "blocked" = "displayed";
  let renderCount = 0;
  let visHandler: ((v: "foreground" | "background") => void) | null = null;
  let schemeHandler: ((s: "light" | "dark") => void) | null = null;
  const downloads: Array<{ url: string; filename?: string; mimeType?: string }> = [];
  // The host reply to stand in: the real host's `{success}` or the
  // simulator's `{ok}`. Defaults to a successful device-shaped reply.
  let downloadReply: Record<string, unknown> = { success: true };

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
        renderCount += 1;
        // A blocked render never reaches the glasses — the previous scene stays.
        if (renderStatus === "displayed") {
          lastScene = elements;
          acceptedRenders.push(elements);
        }
        return Promise.resolve({ status: renderStatus });
      },
    },
    system: {
      openUrl: () => {},
      download: (opts: { url: string; filename?: string; mimeType?: string }) => {
        downloads.push(opts);
        return Promise.resolve(downloadReply);
      },
    },
    colorScheme: "dark" as "light" | "dark",
    onVisibilityChange: (cb: (v: "foreground" | "background") => void) => {
      visHandler = cb;
      return () => {};
    },
    onColorSchemeChange: (cb: (s: "light" | "dark") => void) => {
      schemeHandler = cb;
      return () => {};
    },
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
    renderCount: () => renderCount,
    allRenders: () => acceptedRenders,
    setRenderStatus: (status) => {
      renderStatus = status;
    },
    emitVisibility: (v) => visHandler?.(v),
    emitColorScheme: (s) => schemeHandler?.(s),
    downloads,
    setDownloadReply: (reply) => {
      downloadReply = reply;
    },
  };
}

// ---- fetch stub ------------------------------------------------------------

type Route = (init?: RequestInit) => {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
  /** Hold the response open until this settles — an attacker-paced server. */
  wait?: Promise<void>;
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
    if (out.wait) await out.wait;
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

function makeController(world: FakeWorld, deps: Partial<TenirDeps> = {}): TenirController {
  return new TenirController(world.session, {
    createClient: (url, handlers) => {
      const client = new FakeClient(url, handlers);
      world.clients.push(client);
      return client;
    },
    now: () => new Date(2026, 0, 1, 9, 30),
    ...deps,
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
    expect(c.hudFrame()).toEqual({
      status: "not signed in",
      clock: "",
      caption: SIGN_IN_PROMPT,
      popup: null,
    });
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
    expect(c.hudFrame()).toEqual({
      status: "ready",
      clock: "9:30 AM",
      caption: IDLE_PROMPT,
      popup: null,
    });
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

  it("single tap starts a session; taps while recording do NOTHING (XERK-85)", async () => {
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

    // The upstream safety invariant: a brushed temple must not end a
    // recording — bare taps while recording change nothing.
    world.emitTouch("single_tap");
    world.emitTouch("single_tap");
    expect(client.stopped).toBe(0);
    expect(c.liveState().recording).toBe(true);
    c.stop();
  });

  it("a session ends only through the double-tap menu's confirmed Exit", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const client = world.clients[0];
    client.handlers.onConnectionChange?.("open");

    // Double tap: the Continue/Exit popup, Continue highlighted by default.
    world.emitTouch("double_tap");
    expect(c.hudFrame().popup?.text).toBe("› Continue\n  Exit session");
    // Confirming Continue just dismisses the popup; the session records on.
    world.emitTouch("single_tap");
    expect(c.hudFrame().popup ?? null).toBeNull();
    expect(c.liveState().recording).toBe(true);

    // Double tap again, swipe down to Exit session, tap to confirm.
    world.emitTouch("double_tap");
    world.emitTouch("swipe_down");
    expect(c.hudFrame().popup?.text).toBe("  Continue\n› Exit session");
    world.emitTouch("single_tap");
    expect(client.stopped).toBe(1);
    expect(world.micActive()).toBe(0); // NEVER left subscribed after stop
    expect(c.liveState().recording).toBe(false);
    expect(c.hudFrame().caption).toBe(IDLE_PROMPT);

    // A chunk arriving after stop is dropped, not sent.
    world.emitAudio(new Uint8Array([1, 2]));
    expect(client.audio).toHaveLength(0);
    c.stop();
  });

  it("a second double tap dismisses the menu, same as Continue", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    world.emitTouch("double_tap");
    expect(c.hudFrame().popup?.text).toContain("Continue");
    world.emitTouch("double_tap");
    expect(c.hudFrame().popup ?? null).toBeNull();
    expect(c.liveState().recording).toBe(true);
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
    // The cue goes UP in the box first (XERK-81), anchored after s0.
    expect(c.liveState().activeCue?.afterIndex).toBe(0);
    // Opening the menu embeds the showing cue for review (XERK-108).
    world.emitTouch("double_tap");
    expect(c.liveState().activeCue).toBeNull();
    expect(c.liveState().cues[0].afterIndex).toBe(0);
    world.emitTouch("double_tap"); // close the menu again

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
    // The run also opens the on-lens box, titled with the source language.
    expect(c.hudFrame().popup?.text).toContain("Translating Spanish → English");
    expect(c.hudFrame().popup?.text).toContain("hello");

    h.onSong?.({
      type: "song",
      songId: "sng",
      title: "Weird Fishes",
      artist: "Radiohead",
      atMs: 0,
      offsetMs: 0,
      lines: [],
    });
    // The full LiveSong — lines + anchor — reaches the phone mirror.
    expect(c.liveState().song).toMatchObject({
      id: "sng",
      title: "Weird Fishes",
      artist: "Radiohead",
      lines: [],
      anchorOffsetMs: 0,
    });
    expect(typeof c.liveState().song?.anchorAt).toBe("number");
    // On the lens the song outranks the translation run (XERK-194) and an
    // empty-lyrics song shows the quiet ♪ marker.
    expect(c.hudFrame().popup?.text).toBe("Weird Fishes — Radiohead\n♪ ♪ ♪");
    h.onSongDone?.({ type: "song.done", songId: "sng" });
    expect(c.liveState().song).toBeNull();
    // The still-live translation run retakes the box.
    expect(c.hudFrame().popup?.text).toContain("Translating");
    c.stop();
  });

  it("scrolls the song box's lyric window and re-anchors on song.sync", async () => {
    // The lyric scroll reads the REAL clock off its anchor (upstream
    // currentLyricIndex(song, Date.now())): within this test only a few ms
    // elapse, so the window moves exactly with the sync anchors.
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    h.onSong?.({
      type: "song",
      songId: "sng",
      title: "Song",
      artist: "Artist",
      atMs: 0,
      offsetMs: 0,
      lines: [
        { atMs: 0, text: "line zero" },
        { atMs: 60000, text: "line one" },
        { atMs: 120000, text: "line two" },
      ],
    });
    // At offset 0 the first line is current.
    expect(c.hudFrame().popup?.text).toContain("> line zero");
    // A sync far into the track moves the current line at once.
    h.onSongSync?.({ type: "song.sync", songId: "sng", atMs: 0, offsetMs: 120000 });
    expect(c.hudFrame().popup?.text).toContain("> line two");
    // A sync for some other (stale) run is ignored.
    h.onSongSync?.({ type: "song.sync", songId: "other", atMs: 0, offsetMs: 0 });
    expect(c.hudFrame().popup?.text).toContain("> line two");
    c.stop();
  });

  it("keeps a translation whose turn rolled off the bounded window", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    // A translation for a segment that is NOT in the window any more must
    // still reach the wearer via the box (upstream accumulates regardless).
    h.onTranslation?.({ type: "translation", segmentId: "gone", text: "still shown", sourceLang: "fr" });
    expect(c.hudFrame().popup?.text).toContain("still shown");
    c.stop();
  });

  it("dismisses the translation box on translation.done, draining a queued cue", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    h.onTranslation?.({ type: "translation", segmentId: "s1", text: "uno", sourceLang: "es" });
    // A cue arriving mid-run queues behind the box (XERK-102/XERK-160).
    h.onCue?.({ type: "cue", cueId: "q1", title: "Queued", body: "B", atMs: 0 });
    expect(c.liveState().activeCue).toBeNull();
    h.onTranslationDone?.({ type: "translation.done" });
    // The box frees and the queued cue pops with its countdown running.
    expect(c.liveState().activeCue?.id).toBe("q1");
    expect(c.hudFrame().popup?.text).toContain("Queued");
    c.stop();
  });

  it("cue lifecycle: box + countdown, tap holds it open, menu embeds it", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    h.onCue?.({ type: "cue", cueId: "q1", title: "Employer", body: "Runs the marina.", atMs: 0 });
    const shownAt = c.liveState().activeCue?.shownAt ?? 0;
    expect(shownAt).toBeGreaterThan(0);
    // The box shows title + countdown + body.
    expect(c.hudFrame().popup?.text).toContain("Employer");
    expect(c.hudFrame().popup?.text).toContain("10s");
    expect(c.hudFrame().popup?.text).toContain("Runs the marina.");
    // A second cue queues rather than clobbering the first (XERK-102).
    h.onCue?.({ type: "cue", cueId: "q2", title: "Second", body: "B2", atMs: 0 });
    expect(c.liveState().activeCue?.id).toBe("q1");
    // Any tap or swipe on a live cue restarts its TTL (XERK-129).
    await flush();
    world.emitTouch("single_tap");
    expect(c.liveState().activeCue?.shownAt ?? 0).toBeGreaterThanOrEqual(shownAt);
    expect(c.liveState().recording).toBe(true); // and never stops the session
    c.stop();
  });

  it("pages a long cue body on swipes instead of stranding its tail (XERK-237)", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    // A body far longer than the box's four rows: upstream hands the overflow
    // to a host-scrolled container, which the scene API has no equivalent for.
    const body = Array.from({ length: 12 }, (_, i) => `Row number ${i + 1} of the cue body.`).join(" ");
    h.onCue?.({ type: "cue", cueId: "long", title: "Long", body, atMs: 0 });

    const popup = () => c.hudFrame().popup?.text ?? "";
    const rowsOf = (text: string) => text.split("\n").slice(1); // drop the title row
    const first = rowsOf(popup());
    expect(first.length).toBe(4); // the box still renders exactly four body rows
    expect(popup()).toContain("Row number 1");
    expect(popup()).toContain("▾"); // and says there is more below

    // Swiping down pages the window on; the tail is reachable.
    for (let i = 0; i < 20; i++) world.emitTouch("swipe_down");
    const atEnd = popup();
    expect(atEnd).toContain("Row number 12");
    expect(atEnd).not.toContain("Row number 1 "); // the top rows have scrolled off
    expect(atEnd).not.toContain("▾"); // nothing left below, so no marker

    // Swiping up walks it back, and can never page above the first row.
    for (let i = 0; i < 40; i++) world.emitTouch("swipe_up");
    expect(rowsOf(popup())).toEqual(first);

    // Paging still counts as touching the cue, so it keeps buying time (XERK-129).
    expect(c.liveState().activeCue?.id).toBe("long");
    expect(c.liveState().recording).toBe(true);
    c.stop();
  });

  it("a promoted cue starts at the top of its own body (XERK-237)", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    const long = Array.from({ length: 12 }, (_, i) => `Alpha ${i + 1} padding words here.`).join(" ");
    h.onCue?.({ type: "cue", cueId: "a", title: "A", body: long, atMs: 0 });
    world.emitTouch("swipe_down");
    world.emitTouch("swipe_down");
    // A queued cue takes the box when the menu opens and closes over the first.
    h.onCue?.({ type: "cue", cueId: "b", title: "B", body: "Short body.", atMs: 0 });
    world.emitTouch("double_tap"); // menu opens, embedding cue A
    world.emitTouch("double_tap"); // menu closes, promoting cue B
    expect(c.liveState().activeCue?.id).toBe("b");
    expect(c.hudFrame().popup?.text).toContain("Short body.");
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

    // Clean stop through the menu's confirmed Exit (XERK-85).
    world.emitTouch("double_tap");
    world.emitTouch("swipe_down");
    world.emitTouch("single_tap");
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

  it("re-logs in ONCE however many times the server keeps rejecting (XERK-236)", async () => {
    // `connect()` used to reset the one-shot `reauthAttempted` guard, and the
    // unauthorized handler calls `connect()` after a silent re-login — so the
    // guard was re-armed every time round the loop. Against a real api that was
    // 167 logins + 167 socket upgrades in 15 s, backoff-free, while the lens sat
    // on "connecting to server…" and the wearer was told nothing.
    let logins = 0;
    routes["/auth/login"] = () => {
      logins += 1;
      return { status: 200, body: { token: `tok-${logins}` } };
    };
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    for (let i = 0; i < 20; i += 1) {
      world.clients[world.clients.length - 1].handlers.onError?.({
        type: "error",
        code: "unauthorized",
        message: "rejected",
        fatal: true,
      });
      await flush();
    }
    expect(logins).toBe(1);
    expect(world.clients.length).toBeLessThanOrEqual(2);
    c.stop();
  });

  it("stops the capture on a non-auth error when the session never started (XERK-236)", async () => {
    // What the api really sends when Session()/start() raises — e.g. the STT
    // backend is down. `session.ready` never follows, so the lens read
    // "listening…" with the mic held and PCM going into a socket with no
    // session: nothing recorded, and nothing said so.
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    expect(world.micActive()).toBe(1);
    world.clients[0].handlers.onError?.({
      type: "error",
      code: "internal",
      message: "could not start session",
      fatal: false,
    });
    await flush();
    expect(c.liveState().recording).toBe(false);
    expect(world.micActive()).toBe(0); // the microphone is released
    expect(world.clients[0].stopped).toBe(1);
    c.stop();
  });

  it("leaves a healthy running session alone on a transient error (XERK-236)", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    world.clients[0].handlers.onReady?.({
      type: "session.ready",
      sessionId: "s-1",
      resumed: false,
    });
    await flush();
    world.clients[0].handlers.onError?.({
      type: "error",
      code: "bad_request",
      message: "could not parse message",
      fatal: false,
    });
    await flush();
    expect(c.liveState().recording).toBe(true);
    expect(world.clients[0].stopped).toBe(0);
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
    expect(world.uiSent[0].channel).toBe("tenir:snapshot");
    expect(world.uiSent[0].payload).toMatchObject({
      auth: { signedIn: true, username: "ada" },
      live: { recording: false, connection: "closed" },
    });
    // …and the host's colour scheme alongside it (XERK-237), so a WebView
    // opened after a scheme change doesn't paint in the stale palette.
    expect(world.uiSent.map((m) => m.channel)).toEqual([
      "tenir:snapshot",
      "tenir:color-scheme",
    ]);
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

  it("tenir:login succeeds without the URL global (XERK-216)", async () => {
    // The real background JSContext (JSC / Zipline-QuickJS) has no `URL`.
    // Before the fix, normalizeServerUrl caught the resulting throw and
    // returned "", so a correct address still produced "Enter your server
    // address" — the exact bug reported in XERK-216.
    const savedUrl = Object.getOwnPropertyDescriptor(globalThis, "URL");
    const savedParams = Object.getOwnPropertyDescriptor(globalThis, "URLSearchParams");
    delete (globalThis as Record<string, unknown>).URL;
    delete (globalThis as Record<string, unknown>).URLSearchParams;
    try {
      routes["/auth/login"] = () => ({ status: 200, body: { token: "tok-9" } });
      routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
      const world = makeWorld();
      const c = makeController(world);
      await c.start();
      expect(
        await world.rpc("tenir:login", { serverUrl: "tenir.example.com", username: "ada", password: "pw" }),
      ).toEqual({ ok: true, username: "ada" });
      await flush();
      expect(world.storage.get(SERVER_URL_KEY)).toBe("wss://tenir.example.com/ws");
      expect(fetchCalls.some((f) => f.url === "https://tenir.example.com/auth/login")).toBe(true);
      c.stop();
    } finally {
      if (savedUrl) Object.defineProperty(globalThis, "URL", savedUrl);
      if (savedParams) Object.defineProperty(globalThis, "URLSearchParams", savedParams);
    }
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

  // XERK-237: history audio is upstream's `<audio src>` + download link. The
  // WebView can't fetch the authenticated endpoint cross-origin, but it doesn't
  // need to — the api takes the bearer token as a query param on this route, so
  // the background hands the page a plain playable URL.
  it("tenir:audio-url mints a token-bearing clip URL, and refuses signed out", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    expect(await world.rpc("tenir:audio-url", { id: "conv1" })).toEqual({
      ok: true,
      url: "https://h.example.com/conversations/conv1/audio?token=tok-1",
    });
    // The id is path-encoded: this URL carries the bearer token, so a
    // traversal-shaped id must not be able to steer it off the route.
    const traversal = (await world.rpc("tenir:audio-url", { id: "../../etc/passwd" })) as {
      url: string;
    };
    expect(traversal.url).toBe(
      "https://h.example.com/conversations/..%2F..%2Fetc%2Fpasswd/audio?token=tok-1",
    );
    expect(traversal.url).not.toContain("/../");
    // An id that can't address a conversation is refused rather than turned
    // into `/conversations//audio`.
    expect(await world.rpc("tenir:audio-url", { id: "" })).toEqual({ ok: false });
    expect(await world.rpc("tenir:audio-url", { id: "   " })).toEqual({ ok: false });
    expect(await world.rpc("tenir:download", { id: "" })).toEqual({ ok: false });
    expect(world.downloads).toEqual([]);
    c.stop();

    const out = makeWorld();
    const c2 = makeController(out);
    await c2.start();
    expect(await out.rpc("tenir:audio-url", { id: "conv1" })).toEqual({ ok: false });
    c2.stop();
  });

  // The page hands over a conversation id and nothing else: the background
  // mints the URL. The host's download sheet does NOT scheme-filter the way
  // openUrl does, and the URL carries the bearer token — so a page-supplied URL
  // would be arbitrary network/file egress with the token attached. An
  // allow-list over one looked equivalent but was not: `tenir:login` re-points
  // the api base even when the login FAILS, so the page could move the base and
  // then satisfy the check.
  it("tenir:download mints the clip URL itself from the conversation id", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    expect(await world.rpc("tenir:download", { id: "c1" })).toEqual({ ok: true });
    expect(world.downloads).toEqual([
      {
        url: "https://h.example.com/conversations/c1/audio?token=tok-1",
        filename: "audio.wav",
        mimeType: "audio/wav",
      },
    ]);
    c.stop();
  });

  // XERK-237, the reason `tenir:download` mints its own URL is only half the
  // fix: `tenir:login` used to re-point the api base BEFORE validating, and a
  // failed login left it there while the session stayed signed in. The page
  // could therefore name a host in one call and have every token-bearing URL
  // minted afterwards — the clip, and the download handed to the host's sheet —
  // address it. A failed login must leave the api exactly where it was.
  it("a failed login cannot re-point the api at a server the page names", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    const before = (await world.rpc("tenir:audio-url", { id: "c1" })) as { url: string };
    expect(before.url).toContain("https://h.example.com/");

    routes["/auth/login"] = () => ({ status: 401, body: { detail: "nope" } });
    expect(
      await world.rpc("tenir:login", {
        serverUrl: "attacker.example",
        username: "a",
        password: "b",
      }),
    ).toEqual({ ok: false, error: "Incorrect username or password." });

    // Still signed in to the ORIGINAL server, and still addressing it.
    expect(c.authState().signedIn).toBe(true);
    const after = (await world.rpc("tenir:audio-url", { id: "c1" })) as { url: string };
    expect(after.url).toBe(before.url);
    expect(after.url).not.toContain("attacker.example");
    expect(c.authState().serverUrl).toBe("h.example.com");

    await world.rpc("tenir:download", { id: "c1" });
    expect(world.downloads.every((d) => !d.url.includes("attacker.example"))).toBe(true);
    expect(world.downloads[0].url).toContain("https://h.example.com/");

    // And a failed attempt never persists the URL it was given.
    expect(world.storage.get(SERVER_URL_KEY)).toBe("wss://h.example.com/ws");
    c.stop();
  });

  // XERK-237: a login carries no authority worth proving, and attaching the
  // bearer token handed the wearer's live credential to whatever server was
  // named — turning a mistyped or hostile address into a token leak rather than
  // just a failed sign-in. It is also what made the whole re-point class worth
  // anything to an attacker.
  it("never sends the bearer token to the server a login names", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    fetchCalls.length = 0;

    routes["/auth/login"] = () => ({ status: 401, body: { detail: "nope" } });
    await world.rpc("tenir:login", { serverUrl: "attacker.example", username: "a", password: "b" });

    const loginCalls = fetchCalls.filter((f) => f.url.includes("/auth/login"));
    expect(loginCalls).toHaveLength(1);
    expect(loginCalls[0].url).toBe("https://attacker.example/auth/login");
    const headers = (loginCalls[0].init?.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    expect(JSON.stringify(headers)).not.toContain("tok-1");
    c.stop();
  });

  // A REJECTED response used to be able to replace the wearer's token, so a
  // failed login against a named server left the real server 401ing.
  it("does not adopt a renewed token from a rejected response", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();

    routes["/auth/login"] = () => ({
      status: 401,
      body: { detail: "nope" },
      headers: { "x-renewed-token": "attacker-token" },
    });
    await world.rpc("tenir:login", { serverUrl: "attacker.example", username: "a", password: "b" });
    await flush();
    expect(world.storage.get(TOKEN_KEY)).toBe("tok-1");

    // The clip URL still carries the ORIGINAL token, against the original host.
    const url = (await world.rpc("tenir:audio-url", { id: "c1" })) as { url: string };
    expect(url.url).toBe("https://h.example.com/conversations/c1/audio?token=tok-1");
    c.stop();
  });

  // XERK-237: restoring the base after a failed login was a ROLLBACK, not
  // isolation — the attempt still moved the module-level api base while it was
  // in flight, and the server it named decides how long that is. Acting inside
  // that window reached the same token-bearing download. The attempt now
  // carries its base explicitly and moves nothing shared until it succeeds.
  it("a login in flight cannot move the api base out from under other handlers", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();

    // A server that holds the login open for as long as it likes.
    let release!: () => void;
    const stalled = new Promise<void>((r) => (release = r));
    routes["/auth/login"] = () => ({ status: 401, body: { detail: "nope" }, wait: stalled });

    const inFlight = world.rpc("tenir:login", {
      serverUrl: "attacker.example",
      username: "a",
      password: "b",
    });
    await flush();

    // INSIDE the window: everything still addresses the real server.
    const url = (await world.rpc("tenir:audio-url", { id: "c1" })) as { url: string };
    expect(url.url).toBe("https://h.example.com/conversations/c1/audio?token=tok-1");
    await world.rpc("tenir:download", { id: "c1" });
    expect(world.downloads.every((d) => d.url.startsWith("https://h.example.com/"))).toBe(true);
    expect(c.authState().serverUrl).toBe("h.example.com");

    release();
    expect(await inFlight).toEqual({ ok: false, error: "Incorrect username or password." });
    // …and still afterwards.
    const after = (await world.rpc("tenir:audio-url", { id: "c1" })) as { url: string };
    expect(after.url).toBe(url.url);
    c.stop();
  });

  it("tenir:download refuses while signed out, and never reaches the host", async () => {
    const world = makeWorld();
    const c = makeController(world);
    await c.start();
    expect(await world.rpc("tenir:download", { id: "c1" })).toEqual({ ok: false });
    expect(world.downloads).toEqual([]);
    c.stop();
  });

  // The real host answers {success}; the miniapp simulator answers {ok}.
  // Accepting only one means the harness and the device disagree about whether
  // saving worked — and the wearer gets a failure toast over a good save.
  it("tenir:download accepts either host reply shape", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    world.setDownloadReply({ ok: true }); // the simulator's shape
    expect(await world.rpc("tenir:download", { id: "c1" })).toEqual({ ok: true });
    world.setDownloadReply({ success: true }); // the device's shape
    expect(await world.rpc("tenir:download", { id: "c1" })).toEqual({ ok: true });
    // A sheet the wearer cancels still SUCCEEDED as far as the host is
    // concerned ({success:true, cancelled:true}) — so no failure is reported.
    world.setDownloadReply({ success: true, cancelled: true });
    expect(await world.rpc("tenir:download", { id: "c1" })).toEqual({ ok: true });
    // A genuine failure is reported rather than swallowed.
    world.setDownloadReply({ success: false });
    expect(await world.rpc("tenir:download", { id: "c1" })).toEqual({ ok: false });
    c.stop();
  });

  // XERK-237: upstream's phone page follows `prefers-color-scheme`; this one is
  // told the host's choice, so it has to arrive and to keep arriving.
  it("forwards the host colour scheme on open and on every change", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    await c.start();
    world.uiSent.length = 0;
    world.openUi();
    expect(world.uiSent.find((m) => m.channel === "tenir:color-scheme")?.payload).toEqual({
      scheme: "dark",
    });

    world.uiSent.length = 0;
    (world.session as unknown as { colorScheme: string }).colorScheme = "light";
    world.emitColorScheme("light");
    expect(world.uiSent).toEqual([{ channel: "tenir:color-scheme", payload: { scheme: "light" } }]);
    c.stop();
  });
});

// XERK-216: a display.render can come back "blocked" (another app owns the
// display / transient host failure) — the frame never reaches the glasses.
// The controller must not cache such a frame as shown, or the finished
// transcript stays stuck on the lens after stop and the clock freezes.
describe("lens HUD staleness (XERK-216)", () => {
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
  const element = (world: FakeWorld, id: string) =>
    world.rendered().find((e) => e.id === id)?.text ?? "";

  async function signedIn(deps: Partial<TenirDeps> = {}) {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world, deps);
    await c.start();
    return { world, c };
  }

  it("retries a blocked render so stop wipes the transcript back to idle", async () => {
    const { world, c } = await signedIn({ tickMs: 10 });
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    h.onConnectionChange?.("open");
    h.onFinal?.({ type: "caption.final", segmentId: "s1", text: "hello world", startMs: 0, endMs: 1 });
    expect(element(world, "caption").endsWith("hello world")).toBe(true);

    // The render that should wipe the band gets blocked by the host.
    world.setRenderStatus("blocked");
    // Stop through the menu's confirmed Exit (XERK-85).
    world.emitTouch("double_tap");
    world.emitTouch("swipe_down");
    world.emitTouch("single_tap");
    await flush();
    expect(element(world, "caption").endsWith("hello world")).toBe(true); // stale on the glasses

    // Once the display frees up, the ticker re-issues the idle frame.
    world.setRenderStatus("displayed");
    await sleep(80);
    expect(element(world, "status")).toBe("ready");
    expect(element(world, "caption")).toBe(IDLE_PROMPT);
    c.stop();
  });

  it("re-renders on foreground restore even when the frame is unchanged", async () => {
    const { world, c } = await signedIn();
    await flush();
    const before = world.renderCount();
    world.emitVisibility("background");
    world.emitVisibility("foreground");
    // Another app may have redrawn the glasses while backgrounded — the same
    // frame must be pushed again, not skipped by the frame cache.
    expect(world.renderCount()).toBe(before + 1);
    expect(element(world, "caption")).toBe(IDLE_PROMPT);
    c.stop();
  });

  it("keeps the clock current via the ticker", async () => {
    let nowValue = new Date(2026, 0, 1, 9, 30);
    const { world, c } = await signedIn({ now: () => nowValue, tickMs: 10 });
    expect(element(world, "clock")).toBe("9:30 AM");
    nowValue = new Date(2026, 0, 1, 9, 31);
    await sleep(80);
    expect(element(world, "clock")).toBe("9:31 AM");
    c.stop();
  });

  it("keeps ticking — and the clock current — while backgrounded", async () => {
    // "background" is this app's normal state: the phone is pocketed while
    // the glasses HUD stays live. The ticker must not stop with it.
    let nowValue = new Date(2026, 0, 1, 9, 30);
    const { world, c } = await signedIn({ now: () => nowValue, tickMs: 10 });
    world.emitVisibility("background");
    nowValue = new Date(2026, 0, 1, 9, 31);
    await sleep(80);
    expect(element(world, "clock")).toBe("9:31 AM");
    c.stop();
  });

  it("stop hard-clears the lens before painting the idle frame", async () => {
    const { world, c } = await signedIn();
    world.emitTouch("single_tap");
    const h = world.clients[0].handlers;
    h.onConnectionChange?.("open");
    h.onFinal?.({ type: "caption.final", segmentId: "s1", text: "hello world", startMs: 0, endMs: 1 });
    // Walk the menu to Exit; capture the render log only for the stop itself.
    world.emitTouch("double_tap");
    world.emitTouch("swipe_down");
    const before = world.allRenders().length;
    world.emitTouch("single_tap"); // confirm Exit — stop the session
    const after = world.allRenders().slice(before);
    // The wipe is an explicit empty scene — not a diffed text update that a
    // lossy pipeline can drop — followed by the idle frame. (A gesture also
    // fires a wake-resync render first, so locate the wipe rather than
    // assuming it is the first render.)
    const wipeIdx = after.findIndex((scene) => scene.length === 0);
    expect(wipeIdx).toBeGreaterThanOrEqual(0);
    expect(after[wipeIdx + 1]?.find((e) => e.id === "caption")?.text).toBe(IDLE_PROMPT);
    expect(after[wipeIdx + 1]?.find((e) => e.id === "status")?.text).toBe("ready");
    c.stop();
  });

  it("periodically re-issues an unchanged frame so the HUD self-heals", async () => {
    const { world, c } = await signedIn({ tickMs: 10 });
    await flush();
    const before = world.renderCount();
    await sleep(120); // > FORCE_REPAINT_TICKS ticks with an unchanged idle frame
    expect(world.renderCount()).toBeGreaterThan(before);
    c.stop();
  });
});

// The idle "ready" clock can freeze when the host parks the JSContext
// (frozen timers) — no interval can fix that, so the HUD must resync on every
// wake signal instead (XERK-216, round 2).
describe("wake resync (XERK-216)", () => {
  async function signedIn(deps: Partial<TenirDeps> = {}) {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world, deps);
    await c.start();
    return { world, c };
  }

  it("any gesture forces a repaint even when the frame is unchanged", async () => {
    const { world, c } = await signedIn();
    await flush();
    const before = world.renderCount();
    world.emitTouch("swipe_up"); // does nothing while idle — but must resync
    expect(world.renderCount()).toBe(before + 1);
    c.stop();
  });

  it("a WebView open forces a repaint alongside the snapshot", async () => {
    const { world, c } = await signedIn();
    await flush();
    const before = world.renderCount();
    world.openUi();
    expect(world.renderCount()).toBe(before + 1);
    expect(world.uiSent.some((m) => m.channel === "tenir:snapshot")).toBe(true);
    c.stop();
  });

  it("paints a boot frame before storage/auth resolve", async () => {
    routes["/auth/me"] = () => ({ status: 200, body: PRINCIPAL });
    const world = makeWorld(AUTHED_SEED);
    const c = makeController(world);
    const started = c.start();
    // Synchronously after start(): the "starting…" frame is already out.
    expect(world.allRenders()[0]?.find((e) => e.id === "caption")?.text).toBe("starting…");
    await started;
    c.stop();
  });
});
