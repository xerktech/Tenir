/**
 * The lens controller: the session state machine behind the glasses UI.
 *
 * Sessions are explicit (XERK-85): once signed in the lens idles ("tap to
 * start"); a single click starts a new session, another click stops it (the
 * api finalizes + stores it). While recording the status line reads
 * "listening" with moving dots, the clock container shows the current time,
 * and the caption band holds only the tail that fits the band — nothing
 * overflows, so the host has nothing to scroll.
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
import type { KeyValueStorage } from "../state/storage";
import { CONTAINER, LensTextWriter, clockText, fitCaption, statusLine } from "./layout";

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

export const SIGN_IN_PROMPT = "Not signed in — open the Tenir app on your phone to sign in.";
export const IDLE_PROMPT = "Tap to start a new session.";

type Mutable = {
  sessionId?: string; // authoritative id, persisted so a resume survives backgrounding
  micSource: PersistedSession["micSource"];
  segments: string[]; // finalized turns
  partial: string; // current live hypothesis
  connection: "connecting" | "open" | "closed";
  recording: boolean; // a session is running (XERK-85: started/stopped by click)
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
  };
  let enabled = false; // signed in — clicks act only while enabled
  let foreground = true; // lens visible — the ticker idles while backgrounded
  let tick = 0;

  // ---- lens rendering helpers ------------------------------------------------
  const transcriptText = () => state.segments.join("\n");
  const renderCaption = () => {
    const body = transcriptText();
    const full = state.partial ? `${body}${body ? "\n" : ""}› ${state.partial}` : body;
    // Only the tail that FITS the band (XERK-85): nothing overflows, so the
    // host has nothing to scroll; old text simply falls off the top.
    writer.set(CONTAINER.caption, fitCaption(full.slice(-TRANSCRIPT_MAX_CHARS)));
  };
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
    state.recording = false;
    client?.stop(); // sends session.end, closes, no reconnect
    client = null;
    void capture.stop();
    state.connection = "closed";
    state.sessionId = undefined;
    state.segments = [];
    state.partial = "";
    void store.clear(); // the session is over — nothing to resume anymore
    showIdle();
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

  // The single event subscription (audio + system events).
  const off = bridge.onEvenHubEvent((event: EvenHubEvent) => {
    if (event.audioEvent) {
      if (state.recording) client?.sendAudio(pcmBytes(event.audioEvent));
      return;
    }
    if (event.sysEvent) {
      handleSysEvent(event);
    }
  });

  const cleanup = async () => {
    clearInterval(ticker);
    off();
    await capture.stop();
    client?.stop();
    await store.flush();
  };

  function handleSysEvent(event: EvenHubEvent): void {
    const type = event.sysEvent?.eventType ?? OsEventTypeList.CLICK_EVENT; // zero-omission
    switch (type) {
      case OsEventTypeList.CLICK_EVENT:
        // Single click starts a new session / stops the running one (XERK-85).
        if (!enabled) break;
        if (state.recording) stopSession();
        else startSession();
        break;
      case OsEventTypeList.DOUBLE_CLICK_EVENT:
        // Canonical exit: confirm dialog; real teardown happens on SYSTEM_EXIT.
        void bridge.shutDownPageContainer(1);
        break;
      case OsEventTypeList.FOREGROUND_ENTER_EVENT:
        foreground = true;
        // The host may have redrawn while we were away: repaint everything.
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
