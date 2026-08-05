/**
 * TenirController — the session state machine behind the glasses HUD, ported
 * from the upstream Even Hub app (`even/src/lens/controller.ts` +
 * `even/src/config.ts` + `even/src/state/*`), rebuilt on the Mentra miniapp
 * SDK. Lives inside the per-miniapp JSContext (NOT the WebView) and survives
 * WebView open/close cycles.
 *
 * Transport seam swaps vs. upstream:
 *   Even bridge audioEvent (raw PCM)  -> session.mic.onAudioChunk (base64 →
 *                                        bytes → ApiClient.sendAudio; frames
 *                                        are dropped whenever the socket isn't
 *                                        open — never buffered)
 *   LensTextWriter / LVGL containers  -> session.display.render() scene with
 *                                        stable element ids (see hud.ts); a
 *                                        frame cache drops unchanged renders
 *   sysEvent/textEvent gestures       -> session.input.onTouch: single_tap
 *                                        toggles listening, double_tap clears
 *                                        the caption band (simplified from the
 *                                        upstream tap/menu scheme — no popup
 *                                        overlays on this port)
 *   getLocalStorage/setLocalStorage   -> session.storage (JSON strings)
 *   Same-WebView phone pages          -> session.ui typed channels
 *                                        (src/shared/channels.ts)
 *
 * The network protocol is EXACTLY upstream's: `wss://host/ws?token=…`, binary
 * frames = raw 16k s16le mono PCM, text frames = JSON control/result messages,
 * reconnect backoff 1s·2^n capped at 16s, 1008 close = unauthorized (one
 * silent re-login retry with cached credentials, then disable), sliding token
 * renewal via the `x-renewed-token` REST response header.
 *
 * Not ported (see the spec): the menu/cue/song/translation lens popups (cue,
 * translation and song data still arrive and are mirrored to the phone page),
 * audio download links in history, and the foreground-exit-only resume
 * heuristic — replaced by a simpler persisted `{sessionId, transcript}`
 * snapshot: present on start ⇒ the JSContext died mid-session ⇒ resume; a
 * clean stop (or clean host shutdown) clears it.
 */

import type { MiniappSession, TouchData, UnsubscribeFn } from "@mentra/miniapp/background";
import { base64ToBytes } from "@mentra/miniapp/background";

import {
  ApiError,
  NetworkError,
  describeLoginError,
  login,
  me,
  request,
} from "../../core/api";
import { clearToken, configureTokenStore, getToken } from "../../core/auth";
import { configureApi, httpBaseFromWs } from "../../core/config";
import type { Lang, MicSource } from "../../core/messages";
import { displayServerUrl, normalizeServerUrl } from "../../core/serverUrl";
import { ApiClient, type ApiHandlers, type SessionParams } from "../../core/ws";
import type { Channels } from "../../shared/channels";
import type {
  ProxyFetchResult,
  TenirAuthState,
  TenirCue,
  TenirLiveState,
  TenirSegment,
  TenirSnapshot,
  TenirSong,
} from "../../shared/types";
import { IDLE_PROMPT, SIGN_IN_PROMPT, clockText, fitCaption, hudElements, statusLine } from "../hud";

// ---- storage keys (upstream even/src names, kept for familiarity) ----------
export const SERVER_URL_KEY = "tenir.serverUrl";
export const TOKEN_KEY = "tenir.token";
export const CREDENTIALS_KEY = "tenir.credentials";
export const SESSION_KEY = "tenir.session";

// Cap how many finalized turns we keep (upstream MAX_SEGMENTS).
export const MAX_SEGMENTS = 60;
// Released cues kept for phone review (upstream MAX_PAST_CUES).
export const MAX_PAST_CUES = 60;
// Keep the on-lens transcript bounded (upstream TRANSCRIPT_MAX_CHARS).
export const TRANSCRIPT_MAX_CHARS = 1200;
// The activity ticker: moves the "listening" dots and keeps the clock current.
// ≤1 Hz — the display pipeline coalesces, but we don't spam it (renders are
// frame-deduped anyway).
export const TICK_MS = 1000;
// Debounce for session-snapshot persistence (upstream persist.ts DEBOUNCE_MS).
const PERSIST_DEBOUNCE_MS = 1500;

/** Persisted mid-session snapshot so a JSContext restart can resume. */
export interface PersistedSession {
  sessionId?: string;
  micSource: MicSource;
  transcript: string;
}

export interface Credentials {
  username: string;
  password: string;
}

/** The slice of ApiClient the controller drives — structural, so tests pass a fake. */
export interface CaptureClient {
  start(params: SessionParams, resumeSessionId?: string): void;
  stop(): void;
  sendAudio(pcm: Uint8Array): boolean;
}

export interface TenirDeps {
  /** Api client factory; tests inject a fake to drive captions without a socket. */
  createClient?: (url: string, handlers: ApiHandlers) => CaptureClient;
  /** Clock override for tests. */
  now?: () => Date;
}

type Send = <C extends keyof Channels & string>(channel: C, payload: Channels[C]) => void;
type On = <C extends keyof Channels & string>(
  channel: C,
  cb: (payload: Channels[C]) => void,
) => () => void;
type Handle = (channel: string, handler: (payload: never) => unknown) => () => void;

interface TypedUi {
  send: Send;
  on: On;
  handle: Handle;
  onOpen: (cb: () => void) => () => void;
}

export class TenirController {
  private readonly createClient: (url: string, handlers: ApiHandlers) => CaptureClient;
  private readonly now: () => Date;

  private started = false;
  private readonly unsubs: Array<() => void> = [];
  private ui!: TypedUi;

  // ---- auth state -----------------------------------------------------------
  private signedIn = false;
  private username = "";
  private wsUrl: string | null = null; // canonical ws(s)://host[:port]/path
  private credentials: Credentials | null = null;
  // One silent re-login per unauthorized rejection, so an expired token heals
  // itself without looping against a server that keeps saying no.
  private reauthAttempted = false;

  // ---- session state --------------------------------------------------------
  private recording = false;
  private connection: "connecting" | "open" | "closed" = "closed";
  private sessionId: string | undefined;
  private micSource: MicSource = "g2-microphone";
  private readonly sourceLang: Lang | undefined = undefined; // auto-detect per turn
  private segments: TenirSegment[] = [];
  private partial = "";
  private cues: TenirCue[] = [];
  private song: TenirSong | null = null;

  private client: CaptureClient | null = null;
  private micUnsub: UnsubscribeFn | null = null;

  // ---- ticker / rendering ---------------------------------------------------
  private tick = 0;
  private ticker: ReturnType<typeof setInterval> | null = null;
  private lastFrameKey = "";

  // ---- snapshot persistence -------------------------------------------------
  private persistTimer: ReturnType<typeof setTimeout> | null = null;
  private pendingPersist: PersistedSession | null = null;

  constructor(
    private readonly session: MiniappSession,
    deps: TenirDeps = {},
  ) {
    this.createClient = deps.createClient ?? ((url, handlers) => new ApiClient(url, handlers));
    this.now = deps.now ?? (() => new Date());
  }

  /** Idempotent — safe to call multiple times. */
  async start(): Promise<void> {
    if (this.started) return;
    this.started = true;

    this.ui = this.session.ui as unknown as TypedUi;

    // ---- config + token store (upstream initConfig) -------------------------
    const [persistedUrl, persistedToken, credsRaw] = await Promise.all([
      this.session.storage.get(SERVER_URL_KEY),
      this.session.storage.get(TOKEN_KEY),
      this.session.storage.get(CREDENTIALS_KEY),
    ]);
    this.wsUrl = persistedUrl ? normalizeServerUrl(persistedUrl) || null : null;
    if (this.wsUrl) configureApi({ httpBaseUrl: httpBaseFromWs(this.wsUrl) });
    this.credentials = parseCredentials(credsRaw);
    configureTokenStore(this.deviceTokenStore(persistedToken));

    this.registerUiHandlers();
    this.subscribeInput();
    this.subscribeLifecycle();
    this.startTicker();

    // ---- boot auth (upstream resolveBootAuth) -------------------------------
    await this.resolveBootAuth();

    if (this.signedIn) {
      // A persisted snapshot means the JSContext died mid-session — resume it
      // so the server session continues instead of being orphaned.
      const resume = await this.loadSessionSnapshot();
      if (resume) this.startSession(resume);
      else this.renderHud();
    } else {
      this.renderHud();
    }
    this.sendAuth();
    console.log(
      `Tenir: started (signedIn=${this.signedIn}, server=${this.wsUrl ?? "unset"}, recording=${this.recording})`,
    );
  }

  stop(): void {
    if (!this.started) return;
    this.started = false;
    this.stopTicker();
    this.teardownMic();
    this.client?.stop();
    this.client = null;
    if (this.persistTimer) {
      clearTimeout(this.persistTimer);
      this.persistTimer = null;
    }
    for (const u of this.unsubs) {
      try {
        u();
      } catch {
        /* ignore */
      }
    }
    this.unsubs.length = 0;
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Auth
  // ─────────────────────────────────────────────────────────────────────────

  /**
   * A TokenStore mirroring the token in memory (client-core needs synchronous
   * reads) and persisting writes to `session.storage` in the background —
   * upstream `deviceTokenStore`.
   */
  private deviceTokenStore(initial: string | null) {
    let current = initial;
    return {
      get: () => current,
      set: (token: string) => {
        current = token;
        void this.session.storage.set(TOKEN_KEY, token).catch(() => {});
      },
      clear: () => {
        current = null;
        void this.session.storage.delete(TOKEN_KEY).catch(() => {});
      },
    };
  }

  /**
   * Resolve the boot state: with a configured server, try the cached token
   * (`me()`), then a silent re-login with the cached credentials. A network
   * failure with a cached sign-in resolves signed-in best-effort (the WS
   * reconnects on its own) — upstream's "offline" case.
   */
  private async resolveBootAuth(): Promise<void> {
    if (!this.wsUrl) return;
    const hadSession = getToken() !== null || this.credentials !== null;
    if (getToken() !== null) {
      try {
        const principal = await me();
        this.signedIn = true;
        this.username = principal.username;
        return;
      } catch (err) {
        if (!(err instanceof ApiError)) {
          // Transport-level failure: server unreachable right now. With a
          // cached sign-in, show the app best-effort rather than demanding a
          // password nobody can check.
          if (hadSession) {
            this.signedIn = true;
            this.username = this.credentials?.username ?? "";
          }
          return;
        }
        // 401: token expired/revoked — fall through to a silent re-login.
      }
    }
    const relogged = await this.silentLogin();
    if (relogged) {
      this.signedIn = true;
      this.username = relogged;
    }
  }

  /**
   * Re-login with the cached credentials (e.g. after the stored token
   * expired). Returns the username on success, null when there are no cached
   * credentials or the server rejects them. A fresh token lands in the token
   * store as a `login()` side effect.
   */
  private async silentLogin(): Promise<string | null> {
    if (!this.credentials) return null;
    try {
      const principal = await login(this.credentials.username, this.credentials.password);
      return principal.username;
    } catch {
      return null;
    }
  }

  /** Signed out (401 that a silent re-login couldn't heal): back to the login form. */
  private disable(): void {
    this.signedIn = false;
    if (this.recording) this.stopSession();
    this.renderHud();
    this.sendAuth();
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Session flow (upstream startSession/stopSession/connect)
  // ─────────────────────────────────────────────────────────────────────────

  /** Start a session — fresh, or resuming the persisted one after a restart. */
  private startSession(resume?: PersistedSession): void {
    if (!this.signedIn || !this.wsUrl) return;
    this.recording = true;
    this.sessionId = resume?.sessionId;
    this.micSource = resume?.micSource ?? "g2-microphone";
    // A resumed transcript comes back as a single restored block.
    this.segments = resume?.transcript ? [{ id: "restored", text: resume.transcript }] : [];
    this.partial = "";
    // A new session reviews its own cues from scratch.
    this.cues = [];
    this.song = null;
    this.connect();
    this.sendLive();
  }

  /** Stop the current session: the api finalizes + stores it; the lens idles. */
  private stopSession(): void {
    this.recording = false;
    this.client?.stop(); // sends session.end, closes, no reconnect
    this.client = null;
    this.teardownMic();
    this.connection = "closed";
    this.sessionId = undefined;
    this.segments = [];
    this.partial = "";
    this.cues = [];
    this.song = null;
    void this.clearSessionSnapshot(); // the session is over — nothing to resume
    this.renderHud();
    this.sendLive();
  }

  private connect(): void {
    // Reconnects (e.g. after a re-login) replace the previous client; the
    // session id is kept so the api resumes the same conversation.
    this.client?.stop();
    this.teardownMic();
    this.reauthAttempted = false;
    this.connection = "connecting";
    this.client = this.createClient(this.wsUrl!, this.buildHandlers());
    this.renderHud();
    this.subscribeMic();
    this.client.start({ micSource: this.micSource, sourceLang: this.sourceLang }, this.sessionId);
  }

  private buildHandlers(): ApiHandlers {
    return {
      onConnectionChange: (s) => {
        this.connection = s;
        this.renderHud();
        this.sendLive();
      },
      onReady: (m) => {
        // Capture the authoritative id and persist it so a later restore can
        // resume this same session.
        this.sessionId = m.sessionId;
        this.reauthAttempted = false;
        this.persistSession();
        this.renderHud();
      },
      onPartial: (m) => {
        this.partial = m.text;
        this.renderHud();
        this.sendLive();
      },
      onFinal: (m) => {
        this.segments.push({ id: m.segmentId, text: m.text, lang: m.lang });
        if (this.segments.length > MAX_SEGMENTS) {
          this.segments.shift();
          // The oldest turn fell off the bounded window: shift every embedded
          // cue's anchor to match (upstream XERK-108).
          for (const c of this.cues) c.afterIndex -= 1;
        }
        this.partial = "";
        this.renderHud();
        this.sendLive();
        this.persistSession();
      },
      onCue: (m) => {
        // No lens popup on this port — the cue drops straight into the phone
        // transcript for review, anchored after the turn that triggered it.
        this.cues.push({
          id: m.cueId,
          title: m.title,
          body: m.body,
          source: m.source,
          afterIndex: this.segments.length - 1,
        });
        if (this.cues.length > MAX_PAST_CUES) this.cues.shift();
        this.sendLive();
      },
      onTranslation: (m) => {
        // Pair the English rendering with its turn for the phone mirror.
        const seg = this.segments.find((s) => s.id === m.segmentId);
        if (seg) {
          seg.translation = m.text;
          this.sendLive();
        }
      },
      onSong: (m) => {
        this.song = { id: m.songId, title: m.title, artist: m.artist };
        this.sendLive();
      },
      onSongDone: (m) => {
        if (this.song && this.song.id === m.songId) {
          this.song = null;
          this.sendLive();
        }
      },
      onError: (m) => {
        console.warn("Tenir: api error", m.code, m.message);
        if (m.code === "unauthorized") {
          // Expired/revoked token: re-login silently with the cached
          // credentials and reconnect. Only if that fails does the wearer get
          // sent to the phone login page.
          if (!this.reauthAttempted) {
            this.reauthAttempted = true;
            void this.silentLogin().then((username) => {
              if (username && this.recording) {
                this.username = username;
                this.connect();
              } else if (!username) {
                this.disable();
              }
            });
          } else {
            this.disable();
          }
        }
      },
    };
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Microphone (session.mic → ApiClient binary frames)
  // ─────────────────────────────────────────────────────────────────────────

  private subscribeMic(): void {
    if (this.micUnsub) return;
    this.micUnsub = this.session.mic.onAudioChunk((d) => {
      if (!this.recording || !this.client) return;
      const bytes = base64ToBytes(d.data || "");
      if (bytes.length === 0) return;
      // sendAudio drops the frame when the socket isn't open (or is
      // backpressured) — never buffered.
      this.client.sendAudio(bytes);
    });
  }

  /** NEVER leave the mic subscribed after stop — every teardown path calls this. */
  private teardownMic(): void {
    if (!this.micUnsub) return;
    try {
      this.micUnsub();
    } catch {
      /* ignore */
    }
    this.micUnsub = null;
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Lens HUD (hud.ts geometry; frame-deduped renders)
  // ─────────────────────────────────────────────────────────────────────────

  /** The full transcript text the caption band fits from (bounded). */
  transcriptText(): string {
    const body = this.segments.map((s) => s.text).join("\n");
    const full = this.partial ? `${body}${body ? "\n" : ""}${this.partial}` : body;
    return full.slice(-TRANSCRIPT_MAX_CHARS);
  }

  /** What every HUD element should currently read — the one source of truth. */
  hudFrame(): { status: string; clock: string; caption: string } {
    if (!this.signedIn) {
      return { status: "not signed in", clock: "", caption: SIGN_IN_PROMPT };
    }
    const clock = clockText(this.now());
    if (!this.recording) {
      return { status: "ready", clock, caption: IDLE_PROMPT };
    }
    return {
      status: statusLine({ recording: true, connection: this.connection }, this.tick),
      clock,
      caption: fitCaption(this.transcriptText()),
    };
  }

  private renderHud(): void {
    // No display, nothing to draw (hardware requirement is OPTIONAL).
    const frame = this.hudFrame();
    const key = `${frame.status}\u0000${frame.clock}\u0000${frame.caption}`;
    if (key === this.lastFrameKey) return; // unchanged frame — skip the render
    this.lastFrameKey = key;
    void this.session.display.render(hudElements(frame));
  }

  private startTicker(): void {
    if (this.ticker) return;
    this.ticker = setInterval(() => {
      this.tick += 1;
      // The frame cache means this costs a render only when the clock minute
      // turns over or the "listening" dots actually move.
      this.renderHud();
    }, TICK_MS);
  }

  private stopTicker(): void {
    if (!this.ticker) return;
    clearInterval(this.ticker);
    this.ticker = null;
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Input (single tap toggles listening; double tap clears the band)
  // ─────────────────────────────────────────────────────────────────────────

  private subscribeInput(): void {
    this.unsubs.push(
      this.session.input.onTouch((data: TouchData) => this.handleGesture(data.kind)),
    );
  }

  handleGesture(kind: string): void {
    if (!this.signedIn) return;
    switch (kind) {
      case "single_tap":
        if (this.recording) this.stopSession();
        else this.startSession();
        break;
      case "double_tap":
        // Clear the caption band (and the phone mirror with it) — the session
        // keeps recording; the server transcript is unaffected.
        if (this.recording) {
          this.segments = [];
          this.partial = "";
          this.renderHud();
          this.sendLive();
        }
        break;
      default:
        // Other gestures are ignored.
        break;
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Lifecycle
  // ─────────────────────────────────────────────────────────────────────────

  private subscribeLifecycle(): void {
    try {
      this.unsubs.push(
        this.session.onVisibilityChange((v) => {
          if (v === "background") {
            // Keep captioning (it's the app's purpose) but stop the animated
            // ticker; caption frames still render as messages arrive.
            this.stopTicker();
          } else {
            this.startTicker();
            this.renderHud();
          }
        }),
      );
    } catch {
      /* visibility not available — ticker just keeps running */
    }
    const shutdown = () => {
      // Clean shutdown: end the session so the api finalizes + stores it, and
      // clear the snapshot so the next start doesn't reopen a finished
      // session. (A crash skips this — leaving the snapshot behind is exactly
      // what makes the next start resume.)
      if (this.recording) {
        this.client?.stop();
        this.client = null;
        this.teardownMic();
        void this.clearSessionSnapshot();
      }
      this.stopTicker();
    };
    try {
      this.unsubs.push(this.session.onBeforeDisconnect(() => shutdown()));
    } catch {
      /* ignore */
    }
    try {
      this.unsubs.push(this.session.on("disconnect", () => shutdown()));
    } catch {
      /* ignore */
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Snapshot persistence (upstream state/persist.ts, debounced)
  // ─────────────────────────────────────────────────────────────────────────

  private persistSession(): void {
    this.pendingPersist = {
      sessionId: this.sessionId,
      micSource: this.micSource,
      transcript: this.segments.map((s) => s.text).join("\n").slice(-TRANSCRIPT_MAX_CHARS),
    };
    if (this.persistTimer) return;
    this.persistTimer = setTimeout(() => {
      this.persistTimer = null;
      const state = this.pendingPersist;
      this.pendingPersist = null;
      if (!state) return;
      void this.session.storage.set(SESSION_KEY, JSON.stringify(state)).catch(() => {});
    }, PERSIST_DEBOUNCE_MS);
  }

  private async loadSessionSnapshot(): Promise<PersistedSession | null> {
    try {
      const raw = await this.session.storage.get(SESSION_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as Partial<PersistedSession>;
      if (typeof parsed.transcript !== "string") return null;
      return {
        sessionId: typeof parsed.sessionId === "string" ? parsed.sessionId : undefined,
        micSource: parsed.micSource === "phone-microphone" ? "phone-microphone" : "g2-microphone",
        transcript: parsed.transcript,
      };
    } catch {
      return null;
    }
  }

  private async clearSessionSnapshot(): Promise<void> {
    this.pendingPersist = null;
    if (this.persistTimer) {
      clearTimeout(this.persistTimer);
      this.persistTimer = null;
    }
    try {
      await this.session.storage.delete(SESSION_KEY);
    } catch {
      /* best-effort */
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  // UI bus
  // ─────────────────────────────────────────────────────────────────────────

  authState(): TenirAuthState {
    return {
      signedIn: this.signedIn,
      // Fall back to the cached credentials' username so the login form can
      // prefill it after a sign-out or an expired session (upstream prefill).
      username: this.username || this.credentials?.username || "",
      serverUrl: this.wsUrl ? displayServerUrl(this.wsUrl) : "",
    };
  }

  liveState(): TenirLiveState {
    return {
      recording: this.recording,
      connection: this.connection,
      segments: this.segments.map((s) => ({ ...s })),
      partial: this.partial,
      cues: this.cues.map((c) => ({ ...c })),
      song: this.song ? { ...this.song } : null,
    };
  }

  snapshot(): TenirSnapshot {
    return { auth: this.authState(), live: this.liveState() };
  }

  private sendAuth(): void {
    this.ui.send("tenir:auth", this.authState());
  }

  private sendLive(): void {
    this.ui.send("tenir:live", this.liveState());
  }

  private registerUiHandlers(): void {
    // Full snapshot on every WebView open.
    this.unsubs.push(this.ui.onOpen(() => this.ui.send("tenir:snapshot", this.snapshot())));

    this.unsubs.push(
      this.ui.handle("tenir:login", (payload: Channels["tenir:login"]["req"]) =>
        this.handleLogin(payload),
      ),
    );

    this.unsubs.push(
      this.ui.handle("tenir:logout", async () => {
        // Clears the bearer token (memory + device store) and the cached
        // credentials; the lens shows its sign-in prompt.
        clearToken();
        this.credentials = null;
        try {
          await this.session.storage.delete(CREDENTIALS_KEY);
        } catch {
          /* ignore */
        }
        if (this.recording) this.stopSession();
        this.signedIn = false;
        this.username = "";
        this.renderHud();
        this.sendAuth();
        return { ok: true as const };
      }),
    );

    this.unsubs.push(
      this.ui.handle("tenir:start", () => {
        if (!this.signedIn) return { ok: false, error: "Not signed in." };
        if (!this.recording) this.startSession();
        return { ok: true };
      }),
    );

    this.unsubs.push(
      this.ui.handle("tenir:stop", () => {
        if (this.recording) this.stopSession();
        return { ok: true };
      }),
    );

    this.unsubs.push(
      this.ui.handle(
        "tenir:fetch",
        (payload: Channels["tenir:fetch"]["req"]): Promise<ProxyFetchResult> =>
          this.handleProxyFetch(payload),
      ),
    );

    this.unsubs.push(
      this.ui.on("tenir:open-url", ({ url }) => {
        try {
          this.session.system.openUrl(url);
        } catch (err) {
          console.warn("Tenir: openUrl failed", err);
        }
      }),
    );
  }

  private async handleLogin(payload: Channels["tenir:login"]["req"]): Promise<
    Channels["tenir:login"]["res"]
  > {
    const wsUrl = normalizeServerUrl(payload.serverUrl);
    if (!wsUrl) {
      return { ok: false, error: "Enter your server address, e.g. tenir.example.com" };
    }
    // Persist the URL and repoint the REST client before the login call.
    this.wsUrl = wsUrl;
    configureApi({ httpBaseUrl: httpBaseFromWs(wsUrl) });
    try {
      await this.session.storage.set(SERVER_URL_KEY, wsUrl);
    } catch {
      /* the URL just won't persist */
    }
    try {
      const principal = await login(payload.username.trim(), payload.password);
      // Cache the credentials so the token's expiry never asks the user to
      // type them again — the app re-logs-in silently.
      this.credentials = { username: payload.username.trim(), password: payload.password };
      try {
        await this.session.storage.set(CREDENTIALS_KEY, JSON.stringify(this.credentials));
      } catch {
        /* ignore */
      }
      this.signedIn = true;
      this.username = principal.username;
      this.renderHud();
      this.sendAuth();
      return { ok: true, username: principal.username };
    } catch (err) {
      return { ok: false, error: describeLoginError(err) };
    }
  }

  private async handleProxyFetch(
    payload: Channels["tenir:fetch"]["req"],
  ): Promise<ProxyFetchResult> {
    if (!this.signedIn) return { ok: false, error: "Not signed in." };
    try {
      // Rides the shared request path, so the x-renewed-token sliding renewal
      // applies to proxied calls exactly as it does to the controller's own.
      const data = await request<unknown>(payload.method ?? "GET", payload.path, payload.body);
      return { ok: true, data };
    } catch (err) {
      if (err instanceof ApiError) {
        return { ok: false, error: `${err.status}: ${err.message}`, status: err.status };
      }
      if (err instanceof NetworkError) {
        return { ok: false, error: "could not reach the server" };
      }
      return { ok: false, error: String(err) };
    }
  }
}

function parseCredentials(raw: string | null): Credentials | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<Credentials>;
    if (typeof parsed.username !== "string" || typeof parsed.password !== "string") return null;
    return { username: parsed.username, password: parsed.password };
  } catch {
    return null;
  }
}
