/**
 * tenir — Even Hub app entry.
 *
 * One page, two surfaces (XERK-82): the phone side shows the login page and,
 * once signed in, the embedded Tenir web UI (src/phone/login.ts); the lens side
 * renders live captions through the SDK bridge. Both run from this single
 * WebView — navigating away would kill the lens app, so nothing here ever
 * navigates.
 *
 * Boot order is strict, and tuned for BLE reality (the Even docs: one flaky hop
 * can hang ~30s, and concurrent bridge calls can crash the connection — which
 * presents as the app closing itself):
 *
 *   1. resolve the bridge (short race; plain browser dev falls through)
 *   2. create the startup page container EXACTLY once, FIRST — before any
 *      storage round-trip — so the lens is never blank while BLE dawdles
 *   3. init config from the DEVICE store (browser localStorage does not
 *      survive restarts in this host); every storage call is timeout-bounded
 *   4. wire events + the phone login page
 *
 * All lens text goes through one serialized LensTextWriter. The caption loop is
 * gated on auth: it starts when the phone page reports a sign-in (cached from a
 * previous run, or fresh) and stops on sign-out — until then the lens says
 * plainly that it is not signed in instead of pretending to listen. The live
 * loop is: host mic -> PCM -> ApiClient (WSS) -> api -> caption messages ->
 * textContainerUpgrade on the lens.
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
import { CONTAINER, LensTextWriter, buildStartupContainer, setText, statusLine } from "./lens/layout";
import { initPhoneLogin, queryPhoneLoginElements } from "./phone/login";
import { silentLogin } from "./state/credentials";
import { SessionStore, type PersistedSession } from "./state/persist";
import { BridgeStorage, BrowserStorage, withBleTimeout, type KeyValueStorage } from "./state/storage";

// Keep the on-lens transcript bounded; textContainerUpgrade caps at 2000 chars.
const TRANSCRIPT_MAX_CHARS = 1200;
// Cap how many finalized turns we keep on the lens.
const MAX_SEGMENTS = 60;
// How long to wait for the Even bridge before assuming a plain browser (dev).
const BRIDGE_TIMEOUT_MS = 2000;
// The one-shot startup container gets a longer leash than a routine BLE call —
// without it there is no lens at all.
const STARTUP_CONTAINER_TIMEOUT_MS = 8000;

const SIGN_IN_PROMPT = "Not signed in — open the Tenir app on your phone to sign in.";

type Mutable = {
  sessionId?: string; // authoritative id, persisted so a resume survives backgrounding
  micSource: PersistedSession["micSource"];
  segments: string[]; // finalized turns
  partial: string; // current live hypothesis
  connection: "connecting" | "open" | "closed";
  listening: boolean;
};

/** What the phone login page drives on the lens side. */
interface LensControls {
  connect(): void;
  disconnect(): void;
}

async function main(): Promise<void> {
  // Race the bridge against a short timeout: packaged app inside the Even
  // Realities WebView -> bridge; plain browser (`npm run dev`) -> null.
  const bridgePromise: Promise<EvenAppBridge | null> = waitForEvenAppBridge().catch((err) => {
    console.warn("tenir: Even bridge unavailable, phone-page-only mode:", err);
    return null;
  });
  const bridge = await Promise.race([
    bridgePromise,
    new Promise<null>((resolve) => setTimeout(() => resolve(null), BRIDGE_TIMEOUT_MS)),
  ]);

  let lens: LensControls | null = null;
  let authed = false;

  // Device path: draw the lens BEFORE any storage round-trip (see file header).
  let writer: LensTextWriter | null = null;
  if (bridge) writer = await createLensSurface(bridge);

  // The device store is the only one that survives app restarts on real
  // glasses; it holds the server URL, bearer token, and cached credentials.
  const storage: KeyValueStorage = bridge ? new BridgeStorage(bridge) : new BrowserStorage();
  await initConfig(storage);

  if (bridge && writer) lens = await wireLens(bridge, storage, writer);

  if (!bridge) {
    // The race timed out but the host may just have been slow: if the bridge
    // shows up late, still bring the lens surface up rather than leaving the
    // glasses dark for the whole run.
    void bridgePromise.then(async (late) => {
      if (!late) return;
      const lateWriter = await createLensSurface(late);
      if (!lateWriter) return;
      lens = await wireLens(late, new BridgeStorage(late), lateWriter);
      if (authed) lens.connect();
    });
  }

  await initPhoneLogin(storage, queryPhoneLoginElements(), {
    onAuthed: () => {
      authed = true;
      lens?.connect();
    },
    onSignedOut: () => {
      authed = false;
      lens?.disconnect();
    },
  });
}

/**
 * The one-shot lens layout, first thing after the bridge resolves. Returns the
 * serialized text writer for it, or null when the host refused the container
 * (the phone page still runs).
 */
async function createLensSurface(bridge: EvenAppBridge): Promise<LensTextWriter | null> {
  const result = await withBleTimeout<StartUpPageCreateResult | null>(
    bridge.createStartUpPageContainer(buildStartupContainer()),
    null,
    STARTUP_CONTAINER_TIMEOUT_MS,
  );
  if (result !== StartUpPageCreateResult.success) {
    console.error("createStartUpPageContainer failed:", result);
    return null;
  }
  return new LensTextWriter((container, content) => setText(bridge, container, content));
}

/**
 * Wire the lens: session restore, audio capture and event routing. Returns the
 * connect/disconnect pair the phone login drives; until then the lens keeps its
 * boot text ("starting…") and the phone page resolves the auth state within a
 * moment of this returning.
 */
async function wireLens(
  bridge: EvenAppBridge,
  storage: KeyValueStorage,
  writer: LensTextWriter,
): Promise<LensControls> {
  const store = new SessionStore(bridge);

  const restored = await store.load(); // timeout-bounded (persist.ts)
  const state: Mutable = {
    sessionId: restored?.sessionId,
    micSource: restored?.micSource ?? config.defaultMicSource,
    // Resume the prior transcript as a single restored block.
    segments: restored?.transcript ? [restored.transcript] : [],
    partial: "",
    connection: "closed",
    listening: true,
  };

  // ---- lens rendering helpers ------------------------------------------------
  const transcriptText = () => state.segments.join("\n");
  const renderCaption = () => {
    const body = transcriptText();
    const full = state.partial ? `${body}${body ? "\n" : ""}› ${state.partial}` : body;
    writer.set(CONTAINER.caption, full.slice(-TRANSCRIPT_MAX_CHARS));
  };
  const renderStatus = () => writer.set(CONTAINER.status, statusLine(state));

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
    writer.set(CONTAINER.caption, SIGN_IN_PROMPT);
    writer.set(CONTAINER.status, "not signed in");
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

  return { connect, disconnect };
}

void main().catch((err) => console.error("tenir failed to start:", err));
