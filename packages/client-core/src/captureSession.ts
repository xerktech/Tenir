/**
 * Live phone-mic capture state machine.
 *
 * This is the mobile counterpart to the Even G2 app's `even/src/main.ts` loop:
 * phone mic -> 16 kHz PCM -> `ApiClient` (WSS) -> caption messages -> on-screen
 * render. It is deliberately framework-agnostic — it imports `react`/`react-native`
 * not at all — so the whole transition logic (partial/final captions,
 * reconnect/resume, pause) is unit-tested with a fake api and a fake mic. The thin
 * React hook that binds it to the screen and the device mic lives in `useCapture.ts`.
 *
 * Audio is *only* streamed while `listening` (pause stops upload without tearing the
 * socket down), and uploads ride `ApiClient`'s own backpressure drop, so a slow
 * link sheds audio rather than piling it up.
 */

import type { ApiHandlers } from "./ws";
import type { Lang, MicSource } from "@tenir/contract";

import type { PcmAudioSource } from "./pcmSource";
import { decodeBase64 } from "./pcm";

// Cap how many finalized turns we keep on screen; this just bounds memory.
const MAX_SEGMENTS = 60;

export type Connection = "connecting" | "open" | "closed";

/** One finalized turn. */
export interface CaptureSegment {
  id: string;
  text: string;
}

export interface CaptureState {
  /** A capture session is live (mic running + socket started) vs. idle/stopped. */
  running: boolean;
  connection: Connection;
  /** Streaming audio vs. paused (socket stays open while paused). */
  listening: boolean;
  micSource: MicSource;
  sessionId?: string;
  segments: CaptureSegment[];
  partial: string;
  error?: string;
}

export type CaptureAction =
  | { type: "start"; micSource: MicSource }
  | { type: "connection"; state: Connection }
  | { type: "ready"; sessionId: string }
  | { type: "partial"; text: string }
  | { type: "final"; segmentId: string; text: string }
  | { type: "error"; message: string }
  | { type: "togglePause" }
  | { type: "micSwitch"; micSource: MicSource }
  | { type: "stop" };

export function initialCaptureState(micSource: MicSource): CaptureState {
  return {
    running: false,
    connection: "closed",
    listening: true,
    micSource,
    segments: [],
    partial: "",
  };
}

/** Pure transition function — every state change in the session funnels through here. */
export function reduce(state: CaptureState, action: CaptureAction): CaptureState {
  switch (action.type) {
    case "start":
      // Fresh live session; keep any restored sessionId for resume but clear the view.
      return {
        ...initialCaptureState(action.micSource),
        running: true,
        connection: "connecting",
        sessionId: state.sessionId,
      };
    case "connection":
      return { ...state, connection: action.state };
    case "ready":
      return { ...state, sessionId: action.sessionId };
    case "partial":
      return { ...state, partial: action.text };
    case "final": {
      const segments = [...state.segments, { id: action.segmentId, text: action.text }];
      if (segments.length > MAX_SEGMENTS) segments.shift();
      return { ...state, segments, partial: "" };
    }
    case "error":
      return { ...state, error: action.message };
    case "togglePause":
      // Pausing clears the in-flight hypothesis so a stale partial doesn't linger.
      return { ...state, listening: !state.listening, partial: "" };
    case "micSwitch":
      return { ...state, micSource: action.micSource };
    case "stop":
      // Session over: drop to idle but keep the transcript on screen to read back.
      return { ...state, running: false, listening: true, connection: "closed", partial: "" };
  }
}

/** Minimal slice of `ApiClient` the session drives (so tests can inject a fake). */
export interface ApiLike {
  start(params: { micSource: MicSource; sourceLang?: Lang }, resumeSessionId?: string): void;
  stop(): void;
  sendAudio(pcm: Uint8Array): boolean;
  switchMic(micSource: MicSource): void;
}

export interface CaptureSessionDeps {
  /** Build a api client bound to these handlers (real one news up `ApiClient`). */
  createClient(handlers: ApiHandlers): ApiLike;
  audio: PcmAudioSource;
  /** Resume id persisted per device so a drop/relaunch continues the same session. */
  loadSessionId(): Promise<string | null>;
  saveSessionId(id: string): void;
  clearSessionId(): void;
  defaultMicSource: MicSource;
  sourceLang?: Lang;
}

/**
 * Owns one capture session end to end: permission, mic, api client, and the
 * reduced view state. Subscribe for state updates (the React hook does); call
 * `start`/`stop`/`togglePause`/`switchMic` to drive it.
 */
export class CaptureSession {
  private deps: CaptureSessionDeps;
  private client: ApiLike | null = null;
  private state: CaptureState;
  private listeners = new Set<(s: CaptureState) => void>();

  constructor(deps: CaptureSessionDeps) {
    this.deps = deps;
    this.state = initialCaptureState(deps.defaultMicSource);
  }

  getState(): CaptureState {
    return this.state;
  }

  /** Subscribe to state changes; immediately invoked with the current state. */
  subscribe(listener: (s: CaptureState) => void): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  private dispatch(action: CaptureAction): void {
    const next = reduce(this.state, action);
    if (next === this.state) return;
    this.state = next;
    for (const l of this.listeners) l(next);
  }

  /**
   * Request mic permission, open the api (resuming a persisted session if any),
   * and start streaming. Returns false (with an error in state) if the mic is denied.
   */
  async start(): Promise<boolean> {
    if (this.state.running) return true;

    const granted = await this.deps.audio.requestPermission();
    if (!granted) {
      const message = this.deps.audio.lastPermissionError ?? "Microphone permission denied";
      this.dispatch({ type: "error", message });
      return false;
    }

    const resumeId = (await this.deps.loadSessionId()) ?? undefined;
    const micSource = this.state.micSource;
    this.dispatch({ type: "start", micSource });
    if (resumeId) this.dispatch({ type: "ready", sessionId: resumeId });

    const client = this.deps.createClient({
      onConnectionChange: (state) => this.dispatch({ type: "connection", state }),
      onReady: (m) => {
        this.deps.saveSessionId(m.sessionId);
        this.dispatch({ type: "ready", sessionId: m.sessionId });
      },
      onPartial: (m) => this.dispatch({ type: "partial", text: m.text }),
      onFinal: (m) => this.dispatch({ type: "final", segmentId: m.segmentId, text: m.text }),
      onError: (m) => this.dispatch({ type: "error", message: m.message }),
    });
    this.client = client;

    const started = await this.deps.audio.start((base64Pcm) => {
      // Drop audio while paused; otherwise hand raw PCM to the (backpressure-aware) client.
      if (!this.state.listening) return;
      const pcm = decodeBase64(base64Pcm);
      if (pcm.length) client.sendAudio(pcm);
    });
    if (!started) {
      this.dispatch({ type: "error", message: "Could not start the microphone" });
      await this.stop();
      return false;
    }

    client.start({ micSource, sourceLang: this.deps.sourceLang }, resumeId);
    return true;
  }

  /** Pause/resume audio upload without closing the socket. */
  togglePause(): void {
    if (this.state.running) this.dispatch({ type: "togglePause" });
  }

  /** Switch capture source at runtime so the server knows the acoustics changed. */
  switchMic(micSource: MicSource): void {
    this.dispatch({ type: "micSwitch", micSource });
    this.client?.switchMic(micSource);
  }

  /** End the session: stop the mic, close the socket, and forget the resume id. */
  async stop(): Promise<void> {
    await this.deps.audio.stop();
    this.client?.stop();
    this.client = null;
    this.deps.clearSessionId();
    this.dispatch({ type: "stop" });
  }
}
