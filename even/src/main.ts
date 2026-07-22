/**
 * tenir — Even Hub app entry.
 *
 * One page, two surfaces (XERK-82): the phone side shows the login page and,
 * once signed in, the embedded Tenir web UI (src/phone/login.ts); the lens side
 * renders live captions through the SDK bridge. Both run from this single
 * WebView — navigating away would kill the lens app, so nothing here ever
 * navigates.
 *
 * Boot order is strict: resolve the bridge, init config from the DEVICE store
 * (browser localStorage does not survive restarts in this host), create the
 * startup page container EXACTLY once, and only then start audio / register
 * events. The caption loop is gated on auth: it starts when the phone page
 * reports a sign-in (cached from a previous run, or fresh) and stops on
 * sign-out. The live loop is: host mic -> PCM -> ApiClient (WSS) -> api ->
 * caption messages -> textContainerUpgrade on the lens.
 */

// Fonts for the phone page, matching the web UI (web/src/main.tsx).
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/600.css";

import {
  OsEventTypeList,
  StartUpPageCreateResult,
  type EvenAppBridge,
  type EvenHubEvent,
  waitForEvenAppBridge,
} from "@evenrealities/even_hub_sdk";

import { ApiClient } from "@tenir/client-core";

import { AudioCapture, pcmBytes } from "./audio/capture";
import { config, initConfig } from "./config";
import {
  CONTAINER,
  buildStartupContainer,
  rebuildPlain,
  rebuildWithCue,
  setText,
} from "./lens/layout";
import { initPhoneLogin, queryPhoneLoginElements } from "./phone/login";
import { silentLogin } from "./state/credentials";
import { SessionStore, type PersistedSession } from "./state/persist";
import { BridgeStorage, BrowserStorage, type KeyValueStorage } from "./state/storage";

// Keep the on-lens transcript bounded; textContainerUpgrade caps at 2000 chars.
const TRANSCRIPT_MAX_CHARS = 1200;
// Cap how many finalized turns we keep on the lens.
const MAX_SEGMENTS = 60;
// A cue box stays on the lens this long, then is dismissed (XERK-81).
const CUE_TTL_MS = 10000;
// Bound the on-lens cue text so it fits the bordered box.
const CUE_MAX_CHARS = 160;
// How long to wait for the Even bridge before assuming a plain browser (dev).
const BRIDGE_TIMEOUT_MS = 2000;

const SIGN_IN_PROMPT = "Sign in on your phone to start live captions.";

type Mutable = {
  sessionId?: string; // authoritative id, persisted so a resume survives backgrounding
  micSource: PersistedSession["micSource"];
  segments: string[]; // finalized turns
  partial: string; // current live hypothesis
  connection: "connecting" | "open" | "closed";
  listening: boolean;
  cue: { title: string; body: string } | null; // the cue currently on the lens (XERK-81)
};

/** What the phone login page drives on the lens side. */
interface LensControls {
  connect(): void;
  disconnect(): void;
}

/**
 * Race the Even bridge against a short timeout: packaged app inside the Even
 * Realities WebView -> bridge; plain browser (`npm run dev`) -> null, and only
 * the phone page runs.
 */
async function resolveBridge(): Promise<EvenAppBridge | null> {
  try {
    const timeout = new Promise<null>((resolve) => setTimeout(() => resolve(null), BRIDGE_TIMEOUT_MS));
    return await Promise.race([waitForEvenAppBridge(), timeout]);
  } catch (err) {
    console.warn("tenir: Even bridge unavailable, phone-page-only mode:", err);
    return null;
  }
}

async function main(): Promise<void> {
  const bridge = await resolveBridge();
  // The device store is the only one that survives app restarts on real
  // glasses; it holds the server URL, bearer token, and cached credentials.
  const storage: KeyValueStorage = bridge ? new BridgeStorage(bridge) : new BrowserStorage();
  await initConfig(storage);

  const lens = bridge ? await startLens(bridge, storage) : null;

  await initPhoneLogin(storage, queryPhoneLoginElements(), {
    onAuthed: () => lens?.connect(),
    onSignedOut: () => lens?.disconnect(),
  });
}

/**
 * Bring up the lens surface: the one-shot layout, session restore, audio
 * capture and event wiring. Returns the connect/disconnect pair the phone
 * login drives; until connect() the lens shows the sign-in prompt.
 */
async function startLens(bridge: EvenAppBridge, storage: KeyValueStorage): Promise<LensControls | null> {
  const store = new SessionStore(bridge);

  const restored = await store.load();
  const state: Mutable = {
    sessionId: restored?.sessionId,
    micSource: restored?.micSource ?? config.defaultMicSource,
    // Resume the prior transcript as a single restored block.
    segments: restored?.transcript ? [restored.transcript] : [],
    partial: "",
    connection: "closed",
    listening: true,
    cue: null,
  };
  let cueTimer: ReturnType<typeof setTimeout> | null = null;

  // 1) One-shot layout. Nothing else may run until this succeeds.
  const result = await bridge.createStartUpPageContainer(buildStartupContainer());
  if (result !== StartUpPageCreateResult.success) {
    console.error("createStartUpPageContainer failed:", result);
    return null;
  }

  // ---- lens rendering helpers ------------------------------------------------
  const transcriptText = () => state.segments.join("\n");
  const renderCaption = () => {
    const body = transcriptText();
    const full = state.partial ? `${body}${body ? "\n" : ""}› ${state.partial}` : body;
    void setText(bridge, CONTAINER.caption, full.slice(-TRANSCRIPT_MAX_CHARS));
  };
  const renderStatus = () => {
    const conn = state.connection === "open" ? "•" : state.connection === "connecting" ? "…" : "×";
    const mic = state.micSource === "g2-microphone" ? "g2" : "phone";
    const mode = state.listening ? "listening" : "paused";
    void setText(bridge, CONTAINER.status, `${mode} · ${mic} · ${conn}`);
  };
  const renderCue = () => {
    if (!state.cue) return;
    const text = `${state.cue.title}\n${state.cue.body}`;
    void setText(bridge, CONTAINER.cue, text.slice(0, CUE_MAX_CHARS));
  };

  // A cue appears in the bordered box above the transcript, then auto-dismisses.
  // Border can only be set at (re)build time, so showing/hiding rebuilds the page;
  // rebuild clears content, so every container is repainted afterwards.
  const showCue = async (title: string, body: string): Promise<void> => {
    state.cue = { title, body };
    if (cueTimer) clearTimeout(cueTimer);
    await bridge.rebuildPageContainer(rebuildWithCue());
    renderStatus();
    renderCaption();
    renderCue();
    cueTimer = setTimeout(() => void hideCue(), CUE_TTL_MS);
  };
  const hideCue = async (): Promise<void> => {
    if (cueTimer) {
      clearTimeout(cueTimer);
      cueTimer = null;
    }
    if (!state.cue) return;
    state.cue = null;
    await bridge.rebuildPageContainer(rebuildPlain());
    renderStatus();
    renderCaption();
  };

  const persist = () =>
    store.save({
      sessionId: state.sessionId, // persisted so a resume survives the WebView migration
      micSource: state.micSource,
      transcript: transcriptText().slice(-TRANSCRIPT_MAX_CHARS),
    });

  // 2) Api client + capture, connected only while signed in.
  let client: ApiClient | null = null;
  const capture = new AudioCapture(bridge);
  // One silent re-login per unauthorized rejection, so an expired token heals
  // itself without looping against a server that keeps saying no.
  let reauthAttempted = false;

  const showSignInPrompt = () => {
    void setText(bridge, CONTAINER.caption, SIGN_IN_PROMPT);
    void setText(bridge, CONTAINER.status, "not signed in");
  };

  const disconnect = () => {
    client?.stop();
    client = null;
    void capture.stop();
    state.connection = "closed";
    showSignInPrompt();
  };

  const connect = () => {
    // Reconnects (e.g. after a re-login) replace the previous client; the
    // session id is kept so the api resumes the same conversation.
    client?.stop();
    reauthAttempted = false;
    state.connection = "connecting";
    client = new ApiClient(config.apiWsUrl, {
      onConnectionChange: (s) => {
        state.connection = s;
        renderStatus();
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
      },
      onFinal: (m) => {
        state.segments.push(m.text);
        if (state.segments.length > MAX_SEGMENTS) state.segments.shift();
        state.partial = "";
        renderCaption();
        persist();
      },
      onCue: (m) => {
        // Show the private context cue in the bordered box above the transcript (XERK-81).
        void showCue(m.title, m.body);
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
              else showSignInPrompt();
            });
          } else {
            showSignInPrompt();
          }
        }
      },
    });
    renderStatus();
    renderCaption();
    void capture.start();
    client.start(
      { micSource: state.micSource, sourceLang: config.defaultSourceLang },
      state.sessionId, // resume the prior session if we restored one
    );
  };

  // 3) The single event subscription (audio + system events).
  const off = bridge.onEvenHubEvent((event: EvenHubEvent) => {
    if (event.audioEvent) {
      if (state.listening) client?.sendAudio(pcmBytes(event.audioEvent));
      return;
    }
    if (event.sysEvent) {
      handleSysEvent(event);
    }
  });

  const cleanup = async () => {
    off();
    if (cueTimer) clearTimeout(cueTimer);
    await capture.stop();
    client?.stop();
    await store.flush();
  };

  function handleSysEvent(event: EvenHubEvent): void {
    const type = event.sysEvent?.eventType ?? OsEventTypeList.CLICK_EVENT; // zero-omission
    switch (type) {
      case OsEventTypeList.CLICK_EVENT:
        // Single click toggles pause/resume of the caption stream.
        state.listening = !state.listening;
        state.partial = "";
        renderStatus();
        renderCaption();
        break;
      case OsEventTypeList.DOUBLE_CLICK_EVENT:
        // Canonical exit: confirm dialog; real teardown happens on SYSTEM_EXIT.
        void bridge.shutDownPageContainer(1);
        break;
      case OsEventTypeList.FOREGROUND_EXIT_EVENT:
        void store.flush();
        break;
      case OsEventTypeList.SYSTEM_EXIT_EVENT:
      case OsEventTypeList.ABNORMAL_EXIT_EVENT:
        void cleanup();
        break;
    }
  }

  // 4) Idle until the phone page reports the auth state: a cached sign-in
  // connects immediately (no phone interaction at all); otherwise the lens
  // shows where to sign in.
  showSignInPrompt();
  return { connect, disconnect };
}

void main().catch((err) => console.error("tenir failed to start:", err));
