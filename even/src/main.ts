/**
 * tenir — Even Hub app entry.
 *
 * Boot order is strict: await the bridge, create the startup page container
 * EXACTLY once, and only then start audio / register events. The live caption
 * loop is: host mic -> PCM -> ApiClient (WSS) -> api -> caption messages
 * -> textContainerUpgrade on the lens.
 */

import {
  OsEventTypeList,
  StartUpPageCreateResult,
  type EvenHubEvent,
  waitForEvenAppBridge,
} from "@evenrealities/even_hub_sdk";

import { ApiClient } from "@tenir/client-core";

import { AudioCapture, pcmBytes } from "./audio/capture";
import { config } from "./config";
import { CONTAINER, buildStartupContainer, setText } from "./lens/layout";
import { SessionStore, type PersistedSession } from "./state/persist";

// Keep the on-lens transcript bounded; textContainerUpgrade caps at 2000 chars.
const TRANSCRIPT_MAX_CHARS = 1200;
// Cap how many finalized turns we keep on the lens.
const MAX_SEGMENTS = 60;

type Mutable = {
  sessionId?: string; // authoritative id, persisted so a resume survives backgrounding
  micSource: PersistedSession["micSource"];
  segments: string[]; // finalized turns
  partial: string; // current live hypothesis
  connection: "connecting" | "open" | "closed";
  listening: boolean;
};

async function main(): Promise<void> {
  const bridge = await waitForEvenAppBridge();
  const store = new SessionStore(bridge);

  const restored = await store.load();
  const state: Mutable = {
    sessionId: restored?.sessionId,
    micSource: restored?.micSource ?? config.defaultMicSource,
    // Resume the prior transcript as a single restored block.
    segments: restored?.transcript ? [restored.transcript] : [],
    partial: "",
    connection: "connecting",
    listening: true,
  };

  // 1) One-shot layout. Nothing else may run until this succeeds.
  const result = await bridge.createStartUpPageContainer(buildStartupContainer());
  if (result !== StartUpPageCreateResult.success) {
    console.error("createStartUpPageContainer failed:", result);
    return;
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

  const persist = () =>
    store.save({
      sessionId: state.sessionId, // persisted so a resume survives the WebView migration
      micSource: state.micSource,
      transcript: transcriptText().slice(-TRANSCRIPT_MAX_CHARS),
    });

  // 2) Api client wired to the lens.
  const client = new ApiClient(config.apiWsUrl, {
    onConnectionChange: (s) => {
      state.connection = s;
      renderStatus();
    },
    onReady: (m) => {
      // Capture the authoritative id and persist it so a later restore can resume
      // this same session.
      state.sessionId = m.sessionId;
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
      // The server URL + sign-in live on the companion page (the lens has no input).
      // On an auth rejection, tell the wearer where to fix it instead of failing mute.
      if (m.code === "unauthorized") {
        void setText(bridge, CONTAINER.caption, "Open the Tenir companion page to set your server and sign in.");
        void setText(bridge, CONTAINER.status, "not signed in");
      }
    },
  });

  // 3) Audio capture + the single event subscription (audio + system events).
  const capture = new AudioCapture(bridge);

  const off = bridge.onEvenHubEvent((event: EvenHubEvent) => {
    if (event.audioEvent) {
      if (state.listening) client.sendAudio(pcmBytes(event.audioEvent));
      return;
    }
    if (event.sysEvent) {
      handleSysEvent(event);
    }
  });

  const cleanup = async () => {
    off();
    await capture.stop();
    client.stop();
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

  // 4) Go live.
  renderStatus();
  renderCaption();
  await capture.start();
  client.start(
    { micSource: state.micSource, sourceLang: config.defaultSourceLang },
    state.sessionId, // resume the prior session if we restored one
  );
}

void main().catch((err) => console.error("tenir failed to start:", err));
