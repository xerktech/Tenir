/**
 * The lens controller: the session state machine behind the glasses UI.
 *
 * Sessions are explicit (XERK-85): once signed in the lens idles ("tap to
 * start"); a single tap starts a new session. While one records, a single tap
 * does NOTHING (a brushed temple must not end a recording) — a double tap
 * pops up a native OS LIST (its own container, added via
 * `rebuildPageContainer`) with Continue (default, top) / Exit session, drawn
 * ON TOP of the live captions, which keep flowing untouched underneath. The
 * OS owns the popup's gestures: swiping moves its selection, a tap reports
 * the chosen item back on the listEvent channel, another double tap dismisses
 * (same as Continue). Exit session stops the session (the api finalizes +
 * stores it). While recording the status line reads "listening" with moving
 * dots, the clock container shows the current time, and the caption band
 * holds only the tail that fits the band — nothing overflows, and the band
 * NEVER captures input, so the OS never scroll-targets the session text.
 *
 * Touch gestures reach the app on several channels: `sysEvent`, `textEvent`
 * aimed at the captured text container, and `listEvent` from the popup list —
 * all are routed through one gesture handler (with a short same-gesture
 * dedupe in case a host mirrors a gesture on more than one channel).
 *
 * Lives apart from main.ts (the boot wiring) so the whole machine — clicks,
 * captions, ticker, persistence — runs under test with a stub bridge and a
 * fake api client (`deps.createClient`).
 */

import { OsEventTypeList, type EvenAppBridge, type EvenHubEvent } from "@evenrealities/even_hub_sdk";

import { ApiClient, type ApiHandlers, type SessionParams } from "@tenir/client-core";

import { AudioCapture, pcmBytes } from "../audio/capture";
import { config } from "../config";
import { PhoneTranscript } from "../phone/transcript";
import { silentLogin } from "../state/credentials";
import { SessionStore, type PersistedSession } from "../state/persist";
import { withBleTimeout, type KeyValueStorage } from "../state/storage";
import {
  CONTAINER,
  LensTextWriter,
  MENU_EXIT_INDEX,
  buildMainPage,
  buildMenuPage,
  clockText,
  fitCaption,
  statusLine,
  type PageContents,
} from "./layout";

// Keep the on-lens transcript bounded; textContainerUpgrade caps at 2000 chars.
// fitCaption trims further to what the band can show — this only bounds the
// text we keep, persist, and measure.
const TRANSCRIPT_MAX_CHARS = 1200;
// Cap how many finalized turns we keep on the lens.
const MAX_SEGMENTS = 60;
// The activity ticker (XERK-85): moves the "listening" dots and keeps the
// clock current. Writes are deduped in LensTextWriter, so only frames that
// actually changed cost a BLE round-trip.
export const TICK_MS = 600;
// A host may deliver the same physical gesture on both the sysEvent and the
// textEvent channel; a same-type gesture repeating inside this window is the
// mirror, not a second gesture. (Two intentional taps this close together are
// a double tap and arrive as one DOUBLE_CLICK anyway.)
export const GESTURE_DEDUPE_MS = 200;

// The touch gestures routed through the dedupe (system lifecycle events are not).
const TOUCH_GESTURES: ReadonlySet<OsEventTypeList> = new Set([
  OsEventTypeList.CLICK_EVENT,
  OsEventTypeList.DOUBLE_CLICK_EVENT,
  OsEventTypeList.SCROLL_TOP_EVENT,
  OsEventTypeList.SCROLL_BOTTOM_EVENT,
]);

export const SIGN_IN_PROMPT = "Not signed in — open the Tenir app on your phone to sign in.";
export const IDLE_PROMPT = "Tap to start a new session.";

type Mutable = {
  sessionId?: string; // authoritative id, persisted so a resume survives backgrounding
  micSource: PersistedSession["micSource"];
  segments: string[]; // finalized turns
  partial: string; // current live hypothesis
  connection: "connecting" | "open" | "closed";
  recording: boolean; // a session is running (XERK-85: tap starts, popup exits)
  menu: boolean; // the in-session popup is up
  menuIndex: number; // its OS-reported selection (0 = Continue, the default)
};

/** The slice of ApiClient the controller drives — structural, so tests pass a fake. */
export interface CaptureClient {
  start(params: SessionParams, resumeSessionId?: string): void;
  stop(): void;
  sendAudio(pcm: Uint8Array): boolean;
}

export interface LensDeps {
  /** Api client factory; tests inject a fake to drive captions without a socket. */
  createClient?: (url: string, handlers: ApiHandlers) => CaptureClient;
}

/** What the phone login page drives on the lens side. */
export interface LensControls {
  /** Signed in: resume a persisted mid-session recording, else idle at "tap to start". */
  enable(): void;
  /** Signed out: stop any session and show the sign-in prompt. */
  disable(): void;
}

/**
 * Wire the lens: session restore, audio capture and event routing. Returns the
 * enable/disable pair the phone login drives; until then the lens keeps its
 * boot text ("starting…") and the phone page resolves the auth state within a
 * moment of this returning.
 */
export async function wireLens(
  bridge: EvenAppBridge,
  storage: KeyValueStorage,
  writer: LensTextWriter,
  phoneTranscript: PhoneTranscript | null,
  deps: LensDeps = {},
): Promise<LensControls> {
  const createClient = deps.createClient ?? ((url, handlers) => new ApiClient(url, handlers));
  const store = new SessionStore(bridge);

  // A persisted session means recording was in progress when the app was
  // backgrounded/killed — the first enable() resumes it; otherwise idle.
  let pendingResume: PersistedSession | null = await store.load(); // timeout-bounded (persist.ts)

  const state: Mutable = {
    micSource: pendingResume?.micSource ?? config.defaultMicSource,
    segments: [],
    partial: "",
    connection: "closed",
    recording: false,
    menu: false,
    menuIndex: 0,
  };
  let enabled = false; // signed in — clicks act only while enabled
  let foreground = true; // lens visible — the ticker idles while backgrounded
  let tick = 0;

  // ---- lens rendering helpers ------------------------------------------------
  const transcriptText = () => state.segments.join("\n");
  /** The caption band's live text — always the full band, popup or not. */
  const liveCaption = () => {
    const body = transcriptText();
    const full = state.partial ? `${body}${body ? "\n" : ""}› ${state.partial}` : body;
    // Only the tail that FITS (XERK-85): nothing overflows, so the host has
    // nothing to scroll; old text simply falls off the top. The popup box is
    // created LAST on its page, so it simply draws on top of these lines —
    // the captions underneath keep rendering untouched.
    return fitCaption(full.slice(-TRANSCRIPT_MAX_CHARS));
  };
  /** What every container should currently read — the one source of page truth. */
  const pageContents = (): PageContents => ({
    status: enabled ? statusLine(state, tick) : "not signed in",
    caption: !enabled ? SIGN_IN_PROMPT : state.recording ? liveCaption() : IDLE_PROMPT,
    clock: state.recording ? clockText(new Date()) : "",
  });
  const renderCaption = () => writer.set(CONTAINER.caption, pageContents().caption);
  const renderStatus = () => writer.set(CONTAINER.status, statusLine(state, tick));
  // The clock shows only while a session is recording (XERK-85).
  const renderClock = () =>
    writer.set(CONTAINER.clock, state.recording ? clockText(new Date()) : "");
  const syncPhone = () =>
    phoneTranscript?.update({
      recording: state.recording,
      connection: state.connection,
      segments: state.segments,
      partial: state.partial,
    });

  /**
   * Swap the page between the plain layout and the one with the popup box —
   * `rebuildPageContainer` is the SDK's sanctioned runtime page change. Rides
   * the writer's serialized lane, then re-asserts every container's text so a
   * stale queued write from just before the swap can't land on the new page.
   */
  const rebuildPage = () => {
    const contents = pageContents();
    const page = state.menu ? buildMenuPage(contents) : buildMainPage(contents);
    writer.run(() => withBleTimeout(bridge.rebuildPageContainer(page), false));
    writer.invalidate();
    writer.set(CONTAINER.status, contents.status);
    writer.set(CONTAINER.caption, contents.caption);
    writer.set(CONTAINER.clock, contents.clock);
  };

  const showIdle = () => {
    writer.set(CONTAINER.status, statusLine(state));
    writer.set(CONTAINER.clock, "");
    writer.set(CONTAINER.caption, IDLE_PROMPT);
  };

  const showSignInPrompt = () => {
    writer.set(CONTAINER.status, "not signed in");
    writer.set(CONTAINER.clock, "");
    writer.set(CONTAINER.caption, SIGN_IN_PROMPT);
  };

  const persist = () =>
    store.save({
      sessionId: state.sessionId, // persisted so a resume survives the WebView migration
      micSource: state.micSource,
      transcript: transcriptText().slice(-TRANSCRIPT_MAX_CHARS),
    });

  // Api client + capture, connected only while a session records.
  let client: CaptureClient | null = null;
  const capture = new AudioCapture(bridge);
  // One silent re-login per unauthorized rejection, so an expired token heals
  // itself without looping against a server that keeps saying no.
  let reauthAttempted = false;

  const connect = () => {
    // Reconnects (e.g. after a re-login) replace the previous client; the
    // session id is kept so the api resumes the same conversation.
    client?.stop();
    reauthAttempted = false;
    state.connection = "connecting";
    client = createClient(config.apiWsUrl, {
      onConnectionChange: (s) => {
        state.connection = s;
        renderStatus();
        syncPhone();
      },
      onReady: (m) => {
        // Capture the authoritative id and persist it so a later restore can resume
        // this same session.
        state.sessionId = m.sessionId;
        reauthAttempted = false;
        persist();
        renderStatus();
      },
      onPartial: (m) => {
        state.partial = m.text;
        renderCaption();
        syncPhone();
      },
      onFinal: (m) => {
        state.segments.push(m.text);
        if (state.segments.length > MAX_SEGMENTS) state.segments.shift();
        state.partial = "";
        renderCaption();
        syncPhone();
        persist();
      },
      onError: (m) => {
        console.warn("api error", m.code, m.message);
        if (m.code === "unauthorized") {
          // Expired/revoked token: re-login silently with the cached credentials
          // and reconnect. Only if that fails does the wearer get sent to the phone.
          if (!reauthAttempted) {
            reauthAttempted = true;
            void silentLogin(storage).then((principal) => {
              if (principal) connect();
              else disable();
            });
          } else {
            disable();
          }
        }
      },
    });
    renderStatus();
    renderCaption();
    renderClock();
    void capture.start();
    client.start(
      { micSource: state.micSource, sourceLang: config.defaultSourceLang },
      state.sessionId, // resume the prior session if we restored one
    );
  };

  /** Start a session — fresh, or resuming the persisted one after backgrounding. */
  const startSession = (resume?: PersistedSession) => {
    state.recording = true;
    state.sessionId = resume?.sessionId;
    state.micSource = resume?.micSource ?? config.defaultMicSource;
    // A resumed transcript comes back as a single restored block.
    state.segments = resume?.transcript ? [resume.transcript] : [];
    state.partial = "";
    connect();
    syncPhone();
  };

  /** Stop the current session: the api finalizes + stores it; the lens idles. */
  const stopSession = () => {
    const menuWasOpen = state.menu;
    state.recording = false;
    state.menu = false;
    client?.stop(); // sends session.end, closes, no reconnect
    client = null;
    void capture.stop();
    state.connection = "closed";
    state.sessionId = undefined;
    state.segments = [];
    state.partial = "";
    void store.clear(); // the session is over — nothing to resume anymore
    // Leaving via the popup: rebuild back to the plain page (which also
    // carries the idle texts); otherwise plain idle writes suffice.
    if (menuWasOpen) rebuildPage();
    else showIdle();
    syncPhone();
  };

  const enable = () => {
    enabled = true;
    if (pendingResume) {
      const resume = pendingResume;
      pendingResume = null;
      startSession(resume);
    } else if (!state.recording) {
      showIdle();
    }
  };

  const disable = () => {
    enabled = false;
    if (state.recording) stopSession();
    showSignInPrompt();
    syncPhone();
  };

  // The activity ticker (XERK-85): while recording in the foreground, move the
  // "listening" dots and keep the top-right clock on the current minute. The
  // writer drops unchanged frames, so this costs BLE only when text changes.
  const ticker = setInterval(() => {
    if (!state.recording || !foreground) return;
    tick += 1;
    renderClock();
    if (state.connection === "open") renderStatus();
  }, TICK_MS);

  // The single event subscription (audio + gestures + system events). Touch
  // gestures arrive on the sysEvent channel, as textEvent aimed at the
  // captured text container, or — while the popup is up — as listEvent from
  // the popup list (which also reports the OS-side selection). All feed one
  // handler, deduped per gesture type in case a host mirrors a gesture on
  // more than one channel.
  let lastGesture = { type: -1 as OsEventTypeList | -1, at: 0 };
  const off = bridge.onEvenHubEvent((event: EvenHubEvent) => {
    if (event.audioEvent) {
      if (state.recording) client?.sendAudio(pcmBytes(event.audioEvent));
      return;
    }
    // The popup list's events carry the authoritative selection: remember it
    // before routing the gesture, so a confirm (from ANY channel) acts on it.
    if (event.listEvent && state.menu) {
      const idx = event.listEvent.currentSelectItemIndex;
      if (typeof idx === "number") state.menuIndex = idx;
    }
    const payload = event.sysEvent ?? event.textEvent ?? event.listEvent;
    if (!payload) return;
    const type = payload.eventType ?? OsEventTypeList.CLICK_EVENT; // zero-omission
    if (TOUCH_GESTURES.has(type)) {
      const now = Date.now();
      if (type === lastGesture.type && now - lastGesture.at < GESTURE_DEDUPE_MS) return;
      lastGesture = { type, at: now };
    }
    handleGesture(type);
  });

  const cleanup = async () => {
    clearInterval(ticker);
    off();
    await capture.stop();
    client?.stop();
    await store.flush();
  };

  /** Open the popup page: the OS list on top, captions flowing underneath. */
  const openMenu = () => {
    state.menu = true;
    state.menuIndex = 0; // Continue is the default
    rebuildPage();
  };

  /** Dismiss the popup: back to the plain page. */
  const closeMenu = () => {
    state.menu = false;
    rebuildPage();
  };

  function handleGesture(type: OsEventTypeList): void {
    switch (type) {
      case OsEventTypeList.CLICK_EVENT:
        if (!enabled) break;
        if (state.menu) {
          // In the popup a tap confirms the OS-selected item.
          if (state.menuIndex === MENU_EXIT_INDEX) stopSession();
          else closeMenu();
        } else if (!state.recording) {
          // Idle: a single tap starts a new session.
          startSession();
        }
        // Recording without the popup: single taps do NOTHING (XERK-85 —
        // a brushed temple must not end a recording).
        break;
      case OsEventTypeList.DOUBLE_CLICK_EVENT:
        if (enabled && state.recording) {
          // Pop up Continue (default) / Exit session; a second double tap
          // dismisses, same as Continue.
          if (state.menu) closeMenu();
          else openMenu();
          break;
        }
        // Outside a session: canonical app exit — confirm dialog; real
        // teardown happens on SYSTEM_EXIT.
        void bridge.shutDownPageContainer(1);
        break;
      case OsEventTypeList.SCROLL_TOP_EVENT:
      case OsEventTypeList.SCROLL_BOTTOM_EVENT:
        // Scrolling is entirely the OS's: in the popup it moves the list
        // selection (already recorded from the listEvent above); anywhere
        // else it targets the tiny capture container, never the session text.
        break;
      case OsEventTypeList.FOREGROUND_ENTER_EVENT:
        foreground = true;
        // The host may have redrawn while we were away: repaint the text
        // containers (the popup list, if up, is the OS's to redraw).
        writer.invalidate();
        if (!enabled) showSignInPrompt();
        else if (state.recording) {
          renderStatus();
          renderClock();
          renderCaption();
        } else {
          showIdle();
        }
        break;
      case OsEventTypeList.FOREGROUND_EXIT_EVENT:
        foreground = false;
        void store.flush();
        break;
      case OsEventTypeList.SYSTEM_EXIT_EVENT:
      case OsEventTypeList.ABNORMAL_EXIT_EVENT:
        void cleanup();
        break;
    }
  }

  return { enable, disable };
}
