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
 *   sysEvent/textEvent gestures       -> session.input.onTouch, upstream
 *                                        semantics: idle single_tap starts a
 *                                        session; while recording a tap does
 *                                        NOTHING (XERK-85: a brushed temple
 *                                        must not end a recording) except
 *                                        confirm the menu or hold a cue open;
 *                                        double_tap opens/closes the
 *                                        Continue/Exit menu; swipes move the
 *                                        menu highlight or reset a cue's TTL
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
 * The lens popup layer (menu, cue, translation, song/lyrics — one shared box,
 * precedence menu > song > translation > cue, XERK-194) is ported in full; see
 * the popup section below and hud.ts for the geometry. Still not ported:
 * audio download links in history (needs a browser), the host-scrolled cue
 * body (the scene API has no native scroll — long cue bodies clip to the box),
 * the idle double-tap app exit (no host API), and the foreground-exit-only
 * resume heuristic — replaced by a simpler persisted `{sessionId, transcript}`
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
import { langName } from "../../core/lang";
import {
  CUE_TTL_MS,
  type LiveSong,
  cueSecondsLeft,
  currentLyricIndex,
  lyricWindow,
} from "../../core/live";
import type { Lang, MicSource, Song, SongDone, SongSync } from "../../core/messages";
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
import {
  type CueCard,
  type HudPopup,
  IDLE_PROMPT,
  type MenuChoice,
  SIGN_IN_PROMPT,
  SONG_BODY_LINES,
  TRANSLATION_BODY_LINES,
  cardPopup,
  clockText,
  cueHeight,
  cueRowRangeFor,
  cueTitleLine,
  dots,
  fitCaption,
  fittedPopup,
  hudElements,
  menuPopup,
  occludedCaption,
  songBody,
  songTitle,
  statusLine,
  tailCueBody,
} from "../hud";

// ---- storage keys (upstream even/src names, kept for familiarity) ----------
export const SERVER_URL_KEY = "tenir.serverUrl";
export const TOKEN_KEY = "tenir.token";
export const CREDENTIALS_KEY = "tenir.credentials";
export const SESSION_KEY = "tenir.session";

// Cap how many finalized turns we keep (upstream MAX_SEGMENTS).
export const MAX_SEGMENTS = 60;
// Released cues kept for phone review (upstream MAX_PAST_CUES).
export const MAX_PAST_CUES = 60;
// Only one cue shows at a time (XERK-102): a cue arriving while another owns
// the box is queued; over the cap the stalest waiting cue is dropped.
export const MAX_QUEUED_CUES = 8;
// Keep the on-lens transcript bounded (upstream TRANSCRIPT_MAX_CHARS).
export const TRANSCRIPT_MAX_CHARS = 1200;
// Bound the translation box's accumulated body the same way (XERK-160).
export const TRANSLATION_MAX_CHARS = 1200;
// The translation box's generic title, when the source language is unknown.
export const TRANSLATION_TITLE = "Translating";
// The activity ticker (upstream TICK_MS): moves the "listening" dots, keeps
// the clock current, advances a cue's countdown (XERK-110) and scrolls a
// song's lyrics (XERK-184). Well under a second so the countdown never lags
// the release timer by more than a tick; renders are frame-deduped, so a tick
// costs a render only when something actually changed.
export const TICK_MS = 600;
// Re-issue the current HUD frame every this many ticks even when unchanged.
// A "displayed" render ack means the host accepted the frame, NOT that the
// glasses drew it — a frame lost further down the pipeline would otherwise
// stay lost forever (XERK-216: the finished transcript stuck on the lens).
// 8 ticks ≈ 5s, the same worst-case self-heal as before the 600ms ticker.
export const FORCE_REPAINT_TICKS = 8;
// Debounce for session-snapshot persistence (upstream persist.ts DEBOUNCE_MS).
const PERSIST_DEBOUNCE_MS = 1500;

/**
 * The translation box title (XERK-173): "Translating <Source> → English" once
 * the spoken language is known, else the bare "Translating". While the run is
 * live a `tick` animates trailing dots — the same activity cue as the
 * "listening…" status line — so the title reads as work in progress.
 */
export function translationTitle(sourceLang?: string, tick?: number): string {
  const from = langName(sourceLang);
  const base = from ? `Translating ${from} → English` : TRANSLATION_TITLE;
  return tick == null ? base : `${base}${dots(tick)}`;
}

/** The live translation run (XERK-160): turn renderings stacked in one box. */
interface TranslationRun {
  texts: string[];
  /** The api said the run is over; a done run only lingers while a box that
   *  outranks it (menu or song) hides it — it dismisses when the box frees. */
  done: boolean;
  sourceLang?: string;
}

/** The cue currently up in the box, with its transcript anchor + TTL start. */
interface ActiveCue extends TenirCue {
  shownAt: number;
}

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
  /** Ticker interval override for tests (default TICK_MS). */
  tickMs?: number;
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
  private readonly tickMs: number;

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
  // The shared popup box's occupants, in precedence order menu > song >
  // translation > cue (XERK-194) — upstream lens/controller.ts `Mutable`.
  private menu: MenuChoice | null = null;
  private cue: ActiveCue | null = null;
  private cueQueue: TenirCue[] = [];
  private cueTimer: ReturnType<typeof setTimeout> | null = null;
  private cues: TenirCue[] = []; // released cues embedded for review (XERK-108)
  private translation: TranslationRun | null = null;
  private song: LiveSong | null = null;

  private client: CaptureClient | null = null;
  private micUnsub: UnsubscribeFn | null = null;
  // One loud warning when the host delivers a non-PCM mic format (LC3 mode).
  private badAudioFormatWarned = false;

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
    this.tickMs = deps.tickMs ?? TICK_MS;
  }

  /** Idempotent — safe to call multiple times. */
  async start(): Promise<void> {
    if (this.started) return;
    this.started = true;

    this.ui = this.session.ui as unknown as TypedUi;

    // Paint immediately, before any storage/auth round-trip, so the lens is
    // never blank while boot dawdles (upstream buildStartupContainer /
    // even/src/main.ts boot order).
    void this.session.display.render(
      hudElements({ status: "", clock: "", caption: "starting…", popup: null }),
    );

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
    this.clearCueTimer();
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
    this.menu = null;
    this.cue = null;
    this.cueQueue = [];
    this.clearCueTimer();
    this.cues = [];
    this.translation = null;
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
    this.menu = null;
    this.cue = null;
    this.cueQueue = [];
    this.clearCueTimer();
    this.cues = [];
    this.translation = null;
    this.song = null;
    void this.clearSessionSnapshot(); // the session is over — nothing to resume
    // Hard-clear before painting the idle frame: the wipe must not hinge on
    // the display pipeline correctly diffing one text update — an explicit
    // empty scene removes the transcript even where that diff is lost, and
    // the forced ticker repaint restores the idle frame if this one drops.
    this.lastFrameKey = "";
    void this.session.display.render([]);
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
      onCue: (m) =>
        this.showCue({ id: m.cueId, title: m.title, body: m.body, source: m.source }),
      onTranslation: (m) => this.showTranslation(m.segmentId, m.text, m.sourceLang),
      onTranslationDone: () => this.finishTranslation(),
      onSong: (m) => this.showSong(m),
      onSongSync: (m) => this.syncSong(m),
      onSongDone: (m) => this.finishSong(m),
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
  // The shared popup box (upstream even/src/lens/controller.ts, XERK-194):
  // one box carries the menu, a cue, a translation run, or a song's lyrics —
  // precedence menu > song > translation > cue. The scene renderer redraws the
  // whole frame each time, so upstream's rebuildPage/renderMenu calls collapse
  // into renderHud() here.
  // ─────────────────────────────────────────────────────────────────────────

  private startCueTimer(): void {
    if (this.cueTimer) clearTimeout(this.cueTimer);
    // The countdown (XERK-110) runs off the same instant the release timer does.
    if (this.cue) this.cue.shownAt = Date.now();
    this.cueTimer = setTimeout(() => this.dismissCue(), CUE_TTL_MS);
  }

  private clearCueTimer(): void {
    if (this.cueTimer) clearTimeout(this.cueTimer);
    this.cueTimer = null;
  }

  /** Seconds before the cue in the box auto-dismisses (XERK-110). */
  private cueCountdown(): number {
    return cueSecondsLeft(this.cue ? Date.now() - this.cue.shownAt : 0);
  }

  /** A cue leaving the box drops into the transcript for review (XERK-108). */
  private embedPastCue(cue: TenirCue): void {
    this.cues.push({ ...cue });
    if (this.cues.length > MAX_PAST_CUES) this.cues.shift();
  }

  /**
   * Show a private context cue (XERK-81): the bordered box above the
   * transcript, auto-dismissed after CUE_TTL_MS. Only one cue shows at a time
   * (XERK-102): a cue arriving while the box is owned — by the menu, another
   * cue, a translation run, or a song — is queued and pops when the box frees.
   */
  private showCue(cue: { id: string; title: string; body: string; source?: string }): void {
    // Anchor to the last finalized turn the moment it arrives (XERK-108).
    const anchored: TenirCue = { ...cue, afterIndex: this.segments.length - 1 };
    if (this.menu || this.cue || this.translation || this.song) {
      this.cueQueue.push(anchored);
      // Bound the backlog: over the cap, drop the stalest waiting cue.
      if (this.cueQueue.length > MAX_QUEUED_CUES) this.cueQueue.shift();
      return;
    }
    this.cue = { ...anchored, shownAt: Date.now() };
    this.startCueTimer();
    this.renderHud();
    this.sendLive();
  }

  private dismissCue(): void {
    this.clearCueTimer();
    if (!this.cue) return;
    // The dismissed cue drops into the transcript for review (XERK-108), then
    // the next queued cue (if any) pops immediately (XERK-102).
    this.embedPastCue(this.cue);
    this.promoteQueuedCue();
    this.renderHud();
    this.sendLive();
  }

  /** Pop the next queued cue into the box (or leave it free). */
  private promoteQueuedCue(): void {
    const next = this.cueQueue.shift() ?? null;
    this.cue = next ? { ...next, shownAt: Date.now() } : null;
    if (this.cue) this.startCueTimer();
  }

  /**
   * A touch on the active cue (XERK-129): ANY tap or swipe restarts the
   * auto-dismiss timer — touching the cue means it is being read, and reading
   * buys it more time.
   */
  private touchCue(): void {
    if (!this.cue) return;
    this.startCueTimer();
    this.renderHud();
    this.sendLive(); // the phone card's countdown resets with it
  }

  /**
   * An English translation of one finalized turn arrived (XERK-160): pair it
   * with its turn for the phone mirror, and stack it into the on-lens box.
   * Every turn of a live run appends into the SAME box (XERK-181, tailed to
   * its newest rows, XERK-172). A cue holding the box loses it immediately and
   * is embedded for review; a song outranks the run (XERK-194), which then
   * accumulates hidden and takes the box only when the song ends.
   */
  private showTranslation(segmentId: string, text: string, sourceLang?: string): void {
    // Pair best-effort — but ALWAYS accumulate into the run, so a translation
    // whose turn has rolled off the bounded window is still read, not lost.
    const seg = this.segments.find((s) => s.id === segmentId);
    if (seg) seg.translation = text;
    if (!this.translation || this.translation.done) {
      this.translation = { texts: [text], done: false, sourceLang };
      if (this.cue) {
        this.embedPastCue(this.cue);
        this.cue = null;
        this.clearCueTimer();
      }
    } else {
      this.translation.texts.push(text);
      // Adopt a later turn's source language if the run started without one.
      if (!this.translation.sourceLang && sourceLang) this.translation.sourceLang = sourceLang;
      // Bound the accumulated body: the box tails to its newest rows.
      while (
        this.translation.texts.length > 1 &&
        this.translation.texts.join("\n").length > TRANSLATION_MAX_CHARS
      ) {
        this.translation.texts.shift();
      }
    }
    this.renderHud();
    this.sendLive();
  }

  /**
   * The api says the other language is done being spoken (`translation.done`):
   * dismiss the box at once (XERK-181) — the server already held its silence
   * window, so there is no on-lens countdown. Behind the menu or a live song
   * the finished run waits; closeMenu/dismissSong dismiss it when the box
   * would otherwise become visible.
   */
  private finishTranslation(): void {
    if (!this.translation || this.translation.done) return;
    this.translation.done = true;
    if (!this.menu && !this.song) this.dismissTranslation();
  }

  /** The translation box leaves; queued cues resume now the run is over. */
  private dismissTranslation(): void {
    if (!this.translation) return;
    this.translation = null;
    if (!this.menu && !this.song) this.promoteQueuedCue();
    this.renderHud();
    this.sendLive();
  }

  /**
   * A song was recognized playing (XERK-184): open (or replace) the lyric box.
   * The scroll is client-driven: stamp the anchor now, so
   * `currentLyricIndex(song, now)` reads straight off the real clock and the
   * ticker carries the window forward. Lyrics beat translation beat cues
   * (XERK-194): a showing cue is embedded for review; a live translation run
   * is held, still accumulating. Only the menu outranks the song.
   */
  private showSong(m: Song): void {
    this.song = {
      id: m.songId,
      title: m.title,
      artist: m.artist,
      lines: m.lines,
      anchorAt: Date.now(),
      anchorOffsetMs: m.offsetMs,
      durationMs: m.durationMs,
    };
    if (this.cue) {
      this.embedPastCue(this.cue);
      this.cue = null;
      this.clearCueTimer();
    }
    this.renderHud();
    this.sendLive();
  }

  /**
   * A fresh sync anchor for the current song (`song.sync`): re-anchor its
   * scroll to correct drift. Same song (matched by id), same lyrics — only
   * (anchorAt, offset) move. Repaint now rather than waiting for the tick.
   */
  private syncSong(m: SongSync): void {
    if (!this.song || this.song.id !== m.songId) return;
    this.song.anchorAt = Date.now();
    this.song.anchorOffsetMs = m.offsetMs;
    this.renderHud();
    this.sendLive();
  }

  /** The current song ended (`song.done`): clear the box. */
  private finishSong(m: SongDone): void {
    if (!this.song || this.song.id !== m.songId) return;
    this.dismissSong();
  }

  /**
   * The song box leaves: whatever it outranked resumes (XERK-194). A held
   * translation retakes the box (or, finished while hidden, is dismissed now,
   * draining a queued cue); otherwise a queued cue pops.
   */
  private dismissSong(): void {
    if (!this.song) return;
    this.song = null;
    if (this.menu) {
      this.sendLive();
      return;
    }
    if (this.translation) {
      if (this.translation.done) this.dismissTranslation();
      else {
        this.renderHud();
        this.sendLive();
      }
      return;
    }
    this.promoteQueuedCue();
    this.renderHud();
    this.sendLive();
  }

  /** Open the Continue/Exit popup: a showing cue has had its turn (XERK-108). */
  private openMenu(): void {
    if (this.cue) {
      this.embedPastCue(this.cue);
      this.cue = null;
      this.clearCueTimer();
    }
    this.menu = "continue"; // Continue is the default
    this.renderHud();
    this.sendLive();
  }

  /** Dismiss the popup: whatever ranks highest below it retakes the box. */
  private closeMenu(): void {
    this.menu = null;
    if (this.song) {
      this.renderHud();
      this.sendLive();
      return;
    }
    if (this.translation) {
      // A run that finished while the menu was up is dismissed now; a live
      // one retakes the box and keeps stacking its turns.
      if (this.translation.done) this.dismissTranslation();
      else {
        this.renderHud();
        this.sendLive();
      }
      return;
    }
    this.promoteQueuedCue();
    this.renderHud();
    this.sendLive();
  }

  /** Move the popup highlight (swipe): repaint the box. */
  private moveMenuHighlight(choice: MenuChoice): void {
    if (!this.menu || this.menu === choice) return;
    this.menu = choice;
    this.renderHud();
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Microphone (session.mic → ApiClient binary frames)
  // ─────────────────────────────────────────────────────────────────────────

  private subscribeMic(): void {
    if (this.micUnsub) return;
    this.micUnsub = this.session.mic.onAudioChunk((d) => {
      if (!this.recording || !this.client) return;
      // The wire contract is raw 16k s16le mono PCM. The host can deliver LC3
      // instead depending on the phone's mic mode — streaming that to the STT
      // is undecodable garbage, so fail loudly (once) and drop the frames.
      const format = (d as { format?: string }).format;
      if (format && !/pcm/i.test(format)) {
        if (!this.badAudioFormatWarned) {
          this.badAudioFormatWarned = true;
          console.warn(`Tenir: unsupported mic audio format "${format}" — dropping audio frames`);
        }
        return;
      }
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

  /** The popup box on top, in precedence order menu > song > translation > cue. */
  private popup(): HudPopup | null {
    if (!this.recording) return null;
    if (this.menu) return menuPopup(this.menu);
    if (this.song) {
      const win = lyricWindow(this.song.lines, currentLyricIndex(this.song, Date.now()));
      const title = cueTitleLine({ title: songTitle(this.song.artist, this.song.title), body: "" });
      return fittedPopup(title, songBody(win, SONG_BODY_LINES));
    }
    if (this.translation) {
      const title = cueTitleLine({
        title: translationTitle(this.translation.sourceLang, this.tick),
        body: "",
      });
      // The translation box TAILS its body (XERK-172): new turns arrive at the
      // bottom, older rows fall off the top.
      return fittedPopup(title, tailCueBody(this.translation.texts.join("\n"), TRANSLATION_BODY_LINES));
    }
    if (this.cue) {
      const card: CueCard = { title: this.cue.title, body: this.cue.body };
      return cardPopup(card, { secondsLeft: this.cueCountdown() });
    }
    return null;
  }

  /** What every HUD element should currently read — the one source of truth. */
  hudFrame(): {
    status: string;
    clock: string;
    caption: string;
    popup?: HudPopup | null;
  } {
    if (!this.signedIn) {
      return { status: "not signed in", clock: "", caption: SIGN_IN_PROMPT, popup: null };
    }
    const clock = clockText(this.now());
    if (!this.recording) {
      return { status: "ready", clock, caption: IDLE_PROMPT, popup: null };
    }
    const popup = this.popup();
    if (!popup) {
      return {
        status: statusLine({ recording: true, connection: this.connection }, this.tick),
        clock,
        caption: fitCaption(this.transcriptText()),
        popup: null,
      };
    }
    // The popup covers the status/clock line and the caption rows its box
    // masks: blank whatever sits underneath it (upstream statusContent/
    // clockContent/occludedCaption), while rows below keep flowing.
    const [first, last] = cueRowRangeFor(cueHeight(popup.rows));
    return {
      status: "",
      clock: "",
      caption: occludedCaption(this.transcriptText(), first, last),
      popup,
    };
  }

  private renderHud(opts: { force?: boolean } = {}): void {
    // No display, nothing to draw (hardware requirement is OPTIONAL).
    const frame = this.hudFrame();
    const popupKey = frame.popup ? `${frame.popup.rows}${frame.popup.text}` : "";
    const key = `${frame.status}\u0000${frame.clock}\u0000${frame.caption}\u0000${popupKey}`;
    if (!opts.force && key === this.lastFrameKey) return; // unchanged frame — skip the render
    this.lastFrameKey = key;
    void this.session.display.render(hudElements(frame)).then((result) => {
      // render() never rejects — a frame that didn't reach the glasses comes
      // back as status "blocked" (display arbitration / transient host
      // failure). Drop the cache so the ticker retries the same frame,
      // instead of treating it as on screen forever — a blocked render at
      // stop left the finished transcript stuck on the lens (XERK-216).
      if (result.status !== "displayed" && this.lastFrameKey === key) {
        this.lastFrameKey = "";
      }
    });
  }

  private startTicker(): void {
    if (this.ticker) return;
    this.ticker = setInterval(() => {
      this.tick += 1;
      // Deduped tick renders cost a real render only when the clock minute
      // turns over or the "listening" dots move. Every FORCE_REPAINT_TICKS
      // the current frame is re-issued even when "unchanged": a "displayed"
      // ack only means the host accepted the frame — not that the glasses
      // drew it — so the HUD must converge on its own after a frame is lost
      // anywhere down the pipeline (XERK-216).
      this.renderHud({ force: this.tick % FORCE_REPAINT_TICKS === 0 });
    }, this.tickMs);
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
        if (this.menu) {
          // In the popup a tap confirms the highlighted choice.
          if (this.menu === "exit") this.stopSession();
          else this.closeMenu();
        } else if (!this.recording) {
          // Idle: a single tap starts a new session.
          this.startSession();
        } else if (this.translation || this.song) {
          // A tap on the translation/song box does nothing: neither has a
          // countdown to reset (XERK-181, XERK-184).
        } else if (this.cue) {
          // A tap on a live cue keeps it up (XERK-129): restart the
          // auto-dismiss so the wearer can hold the cue open while reading.
          this.touchCue();
        }
        // Recording with no menu and no cue: single taps do NOTHING (XERK-85
        // — a brushed temple must not end a recording).
        break;
      case "double_tap":
        // While recording: pop up Continue (default) / Exit session; a second
        // double tap dismisses, same as Continue. A session therefore ends
        // only through the popup's confirmed Exit (XERK-85). Outside a
        // session upstream exited the app; the miniapp host has no
        // self-exit API, so an idle double tap does nothing here.
        if (this.recording) {
          if (this.menu) this.closeMenu();
          else this.openMenu();
        }
        break;
      case "swipe_up":
        // Swipe up: highlight the menu's top row (Continue); on a cue, reset
        // its countdown (XERK-133). Translation/song boxes are left alone.
        if (this.menu) this.moveMenuHighlight("continue");
        else if (this.recording && !this.translation && !this.song) this.touchCue();
        break;
      case "swipe_down":
        // Swipe down: highlight the menu's bottom row (Exit session); on a
        // cue, reset its countdown. Translation/song boxes are left alone.
        if (this.menu) this.moveMenuHighlight("exit");
        else if (this.recording && !this.translation && !this.song) this.touchCue();
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
          // The ticker deliberately keeps running while backgrounded — it is
          // the HUD's heartbeat (clock + lost-frame repaints), and "background"
          // is this app's NORMAL state: the phone sits in a pocket while the
          // glasses HUD stays live. Stopping it froze the lens clock and left
          // a lost stop-wipe stuck forever (XERK-216).
          if (v === "foreground") {
            // Another app may have owned and redrawn the display meanwhile —
            // the frame cache no longer reflects the glasses, so force a
            // fresh render (this also brings the clock current immediately).
            this.renderHud({ force: true });
          } else {
            // Backgrounded: flush the debounced session snapshot now
            // (upstream FOREGROUND_EXIT flush) — a kill right after a final
            // must not lose that turn's transcript or a fresh sessionId.
            this.flushPersist();
          }
        }),
      );
    } catch {
      /* visibility not available — nothing to resync on */
    }
    const shutdown = () => {
      // Clean shutdown (upstream cleanup()): end the session so the api
      // finalizes + stores it, clear the snapshot so the next start doesn't
      // reopen a finished session, then tear the whole controller down —
      // ticker, cue timer, mic, and every subscription. (A crash skips this —
      // leaving the snapshot behind is exactly what makes the next start
      // resume.)
      if (this.recording) void this.clearSessionSnapshot();
      this.stop();
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

  /** Write a pending debounced snapshot NOW (upstream store.flush()). */
  private flushPersist(): void {
    if (!this.persistTimer) return;
    clearTimeout(this.persistTimer);
    this.persistTimer = null;
    const state = this.pendingPersist;
    this.pendingPersist = null;
    if (!state) return;
    void this.session.storage.set(SESSION_KEY, JSON.stringify(state)).catch(() => {});
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
      activeCue: this.cue ? { ...this.cue } : null,
      cues: this.cues.map((c) => ({ ...c })),
      // The full LiveSong — lines + scroll anchor — so the phone card scrolls
      // the same lyric window the lens box does (XERK-184).
      song: this.song ? { ...this.song, lines: this.song.lines.map((l) => ({ ...l })) } : null,
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
