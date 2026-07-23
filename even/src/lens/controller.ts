/**
 * The lens controller: the session state machine behind the glasses UI.
 *
 * Sessions are explicit (XERK-85): once signed in the lens idles ("tap to
 * start"); a single tap starts a new session. While one records, a single tap
 * does NOTHING (a brushed temple must not end a recording) — a double tap
 * pops up a bordered full-width strip over the top two lines of the screen
 * (its own container, added via `rebuildPageContainer`) with Continue
 * (default, top) / Exit session; everything the strip covers — status line,
 * clock, the first caption row — is blanked while it is up, and the rest of
 * the transcript keeps flowing below it: swiping moves the highlight, a
 * single tap confirms it, another double tap dismisses (same as Continue).
 * Exit session stops the
 * session (the api finalizes + stores it). Should the popup-page rebuild fail
 * on the host, the menu falls back into the caption band itself, so the
 * wearer is NEVER stranded inside a session. While recording the status line
 * reads "listening" with moving dots, the clock container shows the current
 * time, and the caption band holds only the rows that fit — nothing
 * overflows, and neither the band nor the clock ever captures input, so the
 * OS scroll animation only ever targets the invisible touch overlay.
 *
 * Touch gestures reach the app on two channels: `sysEvent`, and `textEvent`
 * aimed at the captured touch overlay — both are routed through one gesture
 * handler (with a short same-gesture dedupe in case a host mirrors a gesture
 * on both channels).
 *
 * Lives apart from main.ts (the boot wiring) so the whole machine — clicks,
 * captions, ticker, persistence — runs under test with a stub bridge and a
 * fake api client (`deps.createClient`).
 */

import { OsEventTypeList, type EvenAppBridge, type EvenHubEvent } from "@evenrealities/even_hub_sdk";

import { ApiClient, type ApiHandlers, type SessionParams } from "@tenir/client-core";

import { AudioCapture, pcmBytes } from "../audio/capture";
import { config } from "../config";
import { SessionPage } from "../phone/session";
import { silentLogin } from "../state/credentials";
import { SessionStore, type PersistedSession } from "../state/persist";
import { withBleTimeout, type KeyValueStorage } from "../state/storage";
import {
  CONTAINER,
  LensTextWriter,
  buildMainPage,
  buildMenuPage,
  clockText,
  fitCaption,
  menuText,
  occludedCaption,
  statusLine,
  type MenuChoice,
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
  menu: MenuChoice | null; // the in-session popup's highlight; null = closed
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
  sessionPage: SessionPage | null,
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
    menu: null,
  };
  let enabled = false; // signed in — clicks act only while enabled
  let foreground = true; // lens visible — the ticker idles while backgrounded
  let tick = 0;
  // The popup-page rebuild failed on the host: the menu renders inside the
  // caption band instead, so the wearer always has a way out of a session.
  let menuFallback = false;

  // ---- lens rendering helpers ------------------------------------------------
  const transcriptText = () => state.segments.join("\n");
  /** The caption band's live text: full band, or masked under the popup box. */
  const liveCaption = () => {
    // The popup-page rebuild failed: the band itself carries the menu, so the
    // wearer can still exit the session.
    if (state.menu && menuFallback) return menuText(state.menu);
    const body = transcriptText();
    const full = state.partial ? `${body}${body ? "\n" : ""}› ${state.partial}` : body;
    // Only the rows that FIT (XERK-85): nothing overflows, so the host has
    // nothing to scroll; old text simply falls off the top. While the popup is
    // up, the rows its box covers are masked — an opaque popup would hide
    // exactly those — and the rows around it keep flowing.
    const bounded = full.slice(-TRANSCRIPT_MAX_CHARS);
    return state.menu ? occludedCaption(bounded) : fitCaption(bounded);
  };
  // The popup strip covers the status/clock line and the first caption row:
  // whatever it covers is blanked while it is up (fallback mode has no strip).
  const popupCovering = () => state.menu !== null && !menuFallback;
  const statusContent = () =>
    !enabled ? "not signed in" : popupCovering() ? "" : statusLine(state, tick);
  const clockContent = () => (enabled && !popupCovering() ? clockText(new Date()) : "");
  /** What every container should currently read — the one source of page truth. */
  const pageContents = (): PageContents => ({
    status: statusContent(),
    caption: !enabled ? SIGN_IN_PROMPT : state.recording ? liveCaption() : IDLE_PROMPT,
    clock: clockContent(),
  });
  const renderCaption = () => writer.set(CONTAINER.caption, pageContents().caption);
  const renderMenu = () => {
    if (state.menu && !menuFallback) writer.set(CONTAINER.menu, menuText(state.menu));
  };
  const renderStatus = () => writer.set(CONTAINER.status, statusContent());
  // The clock shows whenever signed in — on the idle "ready" page and while
  // recording alike (XERK-85 feedback) — except under the popup strip.
  const renderClock = () => writer.set(CONTAINER.clock, clockContent());
  const syncPhone = () =>
    sessionPage?.update({
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
    const page = state.menu ? buildMenuPage(contents, state.menu) : buildMainPage(contents);
    const openingMenu = state.menu !== null;
    writer.run(async () => {
      const ok = await withBleTimeout(bridge.rebuildPageContainer(page), false);
      if (!ok && openingMenu && state.menu && !menuFallback) {
        // The popup page never appeared (XERK-85: this once stranded the
        // wearer inside a session). Fall back: render the menu inside the
        // caption band, which needs no rebuild at all.
        menuFallback = true;
        writer.set(CONTAINER.caption, menuText(state.menu));
      }
    });
    writer.invalidate();
    writer.set(CONTAINER.status, contents.status);
    writer.set(CONTAINER.caption, contents.caption);
    writer.set(CONTAINER.clock, contents.clock);
    renderMenu();
  };

  const showIdle = () => {
    renderStatus();
    renderClock();
    writer.set(CONTAINER.caption, IDLE_PROMPT);
  };

  const showSignInPrompt = () => {
    renderStatus();
    renderClock();
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
    const menuWasOpen = state.menu !== null;
    state.recording = false;
    state.menu = null;
    menuFallback = false;
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
      syncPhone(); // the phone Session page shows its idle state (XERK-93)
    }
  };

  const disable = () => {
    enabled = false;
    if (state.recording) stopSession();
    showSignInPrompt();
    syncPhone();
  };

  // The activity ticker (XERK-85): while signed in and foregrounded, keep the
  // top-right clock on the current minute (idle "ready" page included), and
  // while recording move the "listening" dots. The writer drops unchanged
  // frames, so this costs BLE only when text changes.
  const ticker = setInterval(() => {
    if (!enabled || !foreground) return;
    tick += 1;
    renderClock();
    if (state.recording && state.connection === "open") renderStatus();
  }, TICK_MS);

  // The single event subscription (audio + gestures + system events). Touch
  // gestures arrive on the sysEvent channel or as textEvent aimed at the
  // captured touch overlay — on-device swipes come as the latter (XERK-85
  // feedback) — so both feed one handler, deduped per gesture type in case a
  // host mirrors a gesture on both channels.
  let lastGesture = { type: -1 as OsEventTypeList | -1, at: 0 };
  const off = bridge.onEvenHubEvent((event: EvenHubEvent) => {
    if (event.audioEvent) {
      if (state.recording) client?.sendAudio(pcmBytes(event.audioEvent));
      return;
    }
    const payload = event.sysEvent ?? event.textEvent;
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

  /** Open the popup page: the bordered box on top, captions flowing around it. */
  const openMenu = () => {
    state.menu = "continue"; // Continue is the default
    rebuildPage();
  };

  /** Dismiss the popup: back to the plain page, captions full-band again. */
  const closeMenu = () => {
    state.menu = null;
    menuFallback = false;
    rebuildPage();
  };

  /** Move the popup highlight (swipe): repaint the box — or the band, in fallback. */
  const moveMenuHighlight = (choice: MenuChoice) => {
    if (!state.menu || state.menu === choice) return;
    state.menu = choice;
    renderMenu();
    if (menuFallback) renderCaption();
  };

  function handleGesture(type: OsEventTypeList): void {
    switch (type) {
      case OsEventTypeList.CLICK_EVENT:
        if (!enabled) break;
        if (state.menu) {
          // In the popup a tap confirms the highlighted choice.
          if (state.menu === "exit") stopSession();
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
        // Swipe up in the popup: highlight the top row (Continue). Anywhere
        // else the gesture lands on the invisible overlay and does nothing.
        moveMenuHighlight("continue");
        break;
      case OsEventTypeList.SCROLL_BOTTOM_EVENT:
        // Swipe down in the popup: highlight the bottom row (Exit session).
        moveMenuHighlight("exit");
        break;
      case OsEventTypeList.FOREGROUND_ENTER_EVENT:
        foreground = true;
        // The host may have redrawn while we were away: repaint everything
        // (the popup box included, if it is up).
        writer.invalidate();
        if (!enabled) showSignInPrompt();
        else if (state.recording) {
          renderStatus();
          renderClock();
          renderCaption();
          renderMenu();
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
