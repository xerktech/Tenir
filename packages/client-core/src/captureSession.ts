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
import type { CueLevel, Lang, MicSource } from "@tenir/contract";

import type { PcmAudioSource } from "./pcmSource";
import { decodeBase64 } from "./pcm";

// Cap how many finalized turns we keep on screen; this just bounds memory.
const MAX_SEGMENTS = 60;
// The active cue is shown above the transcript for this long, then released
// (XERK-81). The history view keeps cues permanently; this is the live surface only.
export const CUE_TTL_MS = 10000;
// Only ONE cue is shown at a time (XERK-102): a cue arriving while another is
// active is queued and pops the moment the active one is released. This bounds
// how deep that backlog can grow — well past any normal conversation — so a
// torrent of cues can't grow the queue without limit. Over the cap, the stalest
// waiting cue is dropped so the freshest still get their turn.
const MAX_QUEUED_CUES = 16;

export type Connection = "connecting" | "open" | "closed";

/** One finalized turn. */
export interface CaptureSegment {
  id: string;
  text: string;
}

/** A private context cue currently shown above the live transcript (XERK-81). */
export interface LiveCue {
  id: string;
  title: string;
  body: string;
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
  /** The cue currently shown above the transcript; released after CUE_TTL_MS (XERK-102). */
  activeCue: LiveCue | null;
  /**
   * Cues waiting behind the active one (FIFO, XERK-102): when the active cue is
   * released, the head of this queue immediately becomes the new active cue.
   */
  queuedCues: LiveCue[];
  error?: string;
}

export type CaptureAction =
  | { type: "start"; micSource: MicSource }
  | { type: "connection"; state: Connection }
  | { type: "ready"; sessionId: string }
  | { type: "partial"; text: string }
  | { type: "final"; segmentId: string; text: string }
  | { type: "cue"; cue: LiveCue }
  | { type: "cueRelease"; id: string }
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
    activeCue: null,
    queuedCues: [],
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
    case "cue": {
      // A re-delivered cue (e.g. on resume) shouldn't duplicate: if its id is
      // already the active or a queued cue, update that entry in place.
      const cue = action.cue;
      if (state.activeCue?.id === cue.id) return { ...state, activeCue: cue };
      const queuedIdx = state.queuedCues.findIndex((c) => c.id === cue.id);
      if (queuedIdx !== -1) {
        const queuedCues = [...state.queuedCues];
        queuedCues[queuedIdx] = cue;
        return { ...state, queuedCues };
      }
      // Nothing showing → this cue becomes active. Otherwise it waits its turn
      // at the back of the queue (XERK-102).
      if (!state.activeCue) return { ...state, activeCue: cue };
      let queuedCues = [...state.queuedCues, cue];
      // Bound the backlog: over the cap, drop the stalest waiting cue.
      if (queuedCues.length > MAX_QUEUED_CUES) {
        queuedCues = queuedCues.slice(queuedCues.length - MAX_QUEUED_CUES);
      }
      return { ...state, queuedCues };
    }
    case "cueRelease": {
      // The active cue's time is up (or it was dismissed): promote the head of
      // the queue immediately, or clear the surface if nothing is waiting. Guard
      // on id so a stale timer can't release a cue that already moved on.
      if (!state.activeCue || state.activeCue.id !== action.id) return state;
      const [next, ...rest] = state.queuedCues;
      return { ...state, activeCue: next ?? null, queuedCues: rest };
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
      // Live cues (active + queued) are ephemeral, so clear them.
      return {
        ...state,
        running: false,
        listening: true,
        connection: "closed",
        partial: "",
        activeCue: null,
        queuedCues: [],
      };
  }
}

/** Minimal slice of `ApiClient` the session drives (so tests can inject a fake). */
export interface ApiLike {
  start(
    params: { micSource: MicSource; sourceLang?: Lang; cueLevel?: CueLevel },
    resumeSessionId?: string,
  ): void;
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
  /** The user's chosen cue aggressiveness, sent on session.start (XERK-81). */
  cueLevel?: CueLevel;
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
  // The single auto-release timer for the active cue (XERK-102): only one cue
  // shows at a time, so only one timer is ever pending. `cueTimerFor` is the id
  // of the cue it targets, so a promoted cue re-arms and stop() can cancel it.
  private cueTimer: ReturnType<typeof setTimeout> | null = null;
  private cueTimerFor: string | null = null;

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
      onCue: (m) => this.showCue({ id: m.cueId, title: m.title, body: m.body }),
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

    client.start(
      { micSource, sourceLang: this.deps.sourceLang, cueLevel: this.deps.cueLevel },
      resumeId,
    );
    return true;
  }

  /**
   * Enqueue a live cue (XERK-81, XERK-102). It shows immediately if nothing is
   * active, otherwise it waits behind the active cue; either way `syncCueTimer`
   * makes sure the current active cue has a release timer running.
   */
  private showCue(cue: LiveCue): void {
    this.dispatch({ type: "cue", cue });
    this.syncCueTimer();
  }

  /**
   * Ensure exactly one release timer is pending, targeting the current active
   * cue. Called after every cue transition: a newly-active cue arms its timer,
   * a promoted cue re-arms, and an empty surface clears it. When the timer
   * fires it releases the active cue (promoting the queue head) and re-syncs so
   * the promoted cue gets its own countdown.
   */
  private syncCueTimer(): void {
    const active = this.state.activeCue;
    if (!active) {
      this.clearCueTimer();
      return;
    }
    if (this.cueTimerFor === active.id) return; // already timing this cue
    this.clearCueTimer();
    this.cueTimerFor = active.id;
    this.cueTimer = setTimeout(() => {
      this.cueTimer = null;
      this.cueTimerFor = null;
      this.dispatch({ type: "cueRelease", id: active.id });
      this.syncCueTimer();
    }, CUE_TTL_MS);
  }

  private clearCueTimer(): void {
    if (this.cueTimer) clearTimeout(this.cueTimer);
    this.cueTimer = null;
    this.cueTimerFor = null;
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
    this.clearCueTimer();
    this.deps.clearSessionId();
    this.dispatch({ type: "stop" });
  }
}
