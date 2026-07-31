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

// The active cue is shown above the transcript for this long, then released
// (XERK-81). The history view keeps cues permanently; this is the live surface only.
export const CUE_TTL_MS = 10000;
// How long a released cue lingers on screen while it fades out (XERK-107). The
// live cue floats *over* the transcript rather than displacing it, so nothing
// reflows when it comes and goes; this is purely how long the fade lasts, and
// every frontend uses the same value so the surfaces feel identical.
export const CUE_EXIT_MS = 160;
// How often a live cue's countdown (XERK-110) recomputes. Comfortably under a
// second so the displayed number turns over within a frame or two of the real
// second boundary — a countdown ticked on a 1000ms interval drifts visibly
// behind the release timer it is supposed to describe.
export const CUE_COUNTDOWN_TICK_MS = 250;
// Only ONE cue is shown at a time (XERK-102): a cue arriving while another is
// still inside its on-screen window waits its turn behind it. The queue drains on
// wall-clock time (XERK-159), so it only actually backs up during a burst of cues
// closer together than the TTL — this bounds how deep that can get. Over the cap
// the stalest waiting cue is dropped so the freshest still get their turn.
const MAX_QUEUED_CUES = 16;
// A released cue drops into the transcript as a reviewable "past cue" (XERK-108);
// this bounds how many we keep so a long session can't grow them without limit.
// Over the cap the oldest past cue falls off, matching how segments are bounded.
const MAX_PAST_CUES = 60;

export type Connection = "connecting" | "open" | "closed";

/** One finalized turn. */
export interface CaptureSegment {
  id: string;
  text: string;
  /** Language the turn was spoken in, as detected by STT (absent = unknown). */
  lang?: Lang;
  /**
   * English translation of a non-English turn (XERK-160). Arrives after the turn
   * itself (the model call runs off the caption path), so it is attached to the
   * already-rendered segment; the UIs show it turn-by-turn under the original.
   */
  translation?: string;
}

/** A private context cue currently shown above the live transcript (XERK-81). */
export interface LiveCue {
  id: string;
  title: string;
  body: string;
  /**
   * Short label of the live source the cue's fact was grounded in (XERK-120),
   * e.g. "BBC News" or "Wikipedia" — shown as a quiet attribution line under
   * the body. Absent when the cue rests on the model's own knowledge.
   */
  source?: string;
  /**
   * The finalized turn this cue lands after once it's reviewed inline in the
   * transcript (XERK-108) — the last segment present when the cue arrived, since
   * a cue reads as landing just after the words that triggered it. `null` (or a
   * segment since scrolled off the bounded window) anchors it before the first
   * surviving turn. Absent until the cue enters the session state.
   */
  afterSegmentId?: string | null;
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
  /** The single cue shown above the transcript; released after its window (XERK-102). */
  activeCue: LiveCue | null;
  /**
   * Cues waiting behind the active one (FIFO, XERK-102): each takes the band in
   * turn for CUE_TTL_MS. The turns run on a continuous wall-clock schedule that
   * keeps advancing while the app is backgrounded (XERK-159), so a cue whose turn
   * came and went while you were away is already released into the transcript on
   * return rather than replaying one-at-a-time over the live conversation.
   */
  queuedCues: LiveCue[];
  /**
   * Wall-clock time (ms since epoch) the active cue's on-screen window closes
   * (XERK-159). Release is scheduled against real time, not app-foreground time,
   * so the queue reconciles correctly after a spell in the background. `null` when
   * nothing is in the band.
   */
  activeCueEndsAt: number | null;
  /**
   * Cues that have already had their turn in the band and now sit inline in the
   * transcript for review (XERK-108), each anchored to the turn it followed. The
   * live band shows only the active/queued cues, so past cues can be re-read
   * without interfering with cues still coming in.
   */
  pastCues: LiveCue[];
  error?: string;
}

export type CaptureAction =
  | { type: "start"; micSource: MicSource }
  | { type: "connection"; state: Connection }
  | { type: "ready"; sessionId: string }
  | { type: "partial"; text: string }
  | { type: "final"; segmentId: string; text: string; lang?: Lang }
  // English translation of an earlier finalized turn (XERK-160), paired by id.
  | { type: "translation"; segmentId: string; text: string }
  // `now` is the wall-clock arrival time, so the cue's on-screen window is timed
  // against real time and survives the app being backgrounded (XERK-159).
  | { type: "cue"; cue: LiveCue; now: number }
  // Reconcile the cue schedule to wall-clock `now`: release every cue whose turn
  // has elapsed (chaining promotions) and clear or re-seat the band. Fired by the
  // release timer and on returning to the foreground.
  | { type: "cueTick"; now: number }
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
    activeCueEndsAt: null,
    pastCues: [],
  };
}

/**
 * Append a cue to the transcript's past cues (XERK-108) as it leaves the band.
 * Deduped by id so a stale timer or a resume re-release can't double it, and
 * bounded so a long session can't grow the list without limit (the oldest falls
 * off, matching how segments are bounded).
 */
function appendPastCue(pastCues: LiveCue[], cue: LiveCue): LiveCue[] {
  if (pastCues.some((c) => c.id === cue.id)) return pastCues;
  const next = [...pastCues, cue];
  return next.length > MAX_PAST_CUES ? next.slice(next.length - MAX_PAST_CUES) : next;
}

/** The band-facing slice of the cue state, advanced together as the schedule runs. */
type CueBand = Pick<CaptureState, "activeCue" | "queuedCues" | "activeCueEndsAt" | "pastCues">;

/**
 * Advance the cue schedule to wall-clock `now` (XERK-159): while the active cue's
 * window has closed, release it into the transcript and promote the queue head to
 * take over. Each promotion's window opens at the *previous* window's scheduled
 * close (`endsAt + CUE_TTL_MS`), not at `now`, so the turns run on one continuous
 * timeline: catching up after a spell in the background drains every cue whose
 * turn already passed in a single pass, leaving at most the one cue still inside
 * its window (shown for the time it has left) and clearing the band once the
 * queue empties. Returns the same field references when nothing has elapsed, so
 * the reducer can hand back the untouched state.
 */
function advanceCues(state: CaptureState, now: number): CueBand {
  let { activeCue, queuedCues, activeCueEndsAt, pastCues } = state;
  while (activeCue && activeCueEndsAt != null && now >= activeCueEndsAt) {
    pastCues = appendPastCue(pastCues, activeCue);
    if (queuedCues.length > 0) {
      const [next, ...rest] = queuedCues;
      activeCue = next;
      queuedCues = rest;
      activeCueEndsAt = activeCueEndsAt + CUE_TTL_MS;
    } else {
      activeCue = null;
      activeCueEndsAt = null;
    }
  }
  return { activeCue, queuedCues, activeCueEndsAt, pastCues };
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
      // Keep every finalized turn for the whole live session — the transcript
      // scrolls in its own box (XERK-103) and is meant to be scrolled back and
      // re-read (XERK-108), exactly like the history view, which retains the full
      // transcript. An earlier "memory bound" dropped the oldest turns mid-session
      // (XERK-135): because past cues are bounded separately and a conversation
      // yields far more turns than cues, segments rolled off while the cues
      // anchored to them lingered and floated to the top — so scrolling up showed
      // all the cue data but no transcript. Caption text is tiny, so retaining it
      // costs nothing next to the audio being streamed; correctness wins.
      const segments = [
        ...state.segments,
        { id: action.segmentId, text: action.text, lang: action.lang },
      ];
      return { ...state, segments, partial: "" };
    }
    case "translation": {
      // Attach the (later-arriving) translation to its turn (XERK-160). A
      // translation for a turn no longer in view decides nothing.
      const idx = state.segments.findIndex((s) => s.id === action.segmentId);
      if (idx === -1) return state;
      const segments = [...state.segments];
      segments[idx] = { ...segments[idx], translation: action.text };
      return { ...state, segments };
    }
    case "cue": {
      const cue = action.cue;
      // A re-delivered cue (e.g. on resume) shouldn't duplicate: if its id is
      // already the active or a queued cue, update its text in place but keep the
      // transcript anchor and schedule it was first pinned to.
      if (state.activeCue?.id === cue.id) {
        return { ...state, activeCue: { ...cue, afterSegmentId: state.activeCue.afterSegmentId } };
      }
      const queuedIdx = state.queuedCues.findIndex((c) => c.id === cue.id);
      if (queuedIdx !== -1) {
        const queuedCues = [...state.queuedCues];
        queuedCues[queuedIdx] = { ...cue, afterSegmentId: queuedCues[queuedIdx].afterSegmentId };
        return { ...state, queuedCues };
      }
      // Already reviewed and sitting in the transcript (a resume can re-deliver a
      // long-released cue): don't replay it into the band (XERK-108).
      if (state.pastCues.some((c) => c.id === cue.id)) return state;
      // First advance the schedule to the arrival time so any earlier cue whose
      // window has already closed (e.g. while the app was backgrounded) is retired
      // into the transcript before this one is placed (XERK-159).
      const band = advanceCues(state, action.now);
      // Anchor a fresh cue to the last finalized turn so it reads as landing just
      // after the words that triggered it (XERK-108). No turns yet → null, i.e. it
      // will sit before the transcript.
      const anchored: LiveCue = {
        ...cue,
        afterSegmentId: state.segments.length ? state.segments[state.segments.length - 1].id : null,
      };
      // Empty band → this cue shows now, its window running from arrival.
      if (!band.activeCue) {
        return { ...state, ...band, activeCue: anchored, activeCueEndsAt: action.now + CUE_TTL_MS };
      }
      // Otherwise it waits its turn at the back of the queue (XERK-102). Bound the
      // backlog: over the cap, drop the stalest waiting cue.
      let queuedCues = [...band.queuedCues, anchored];
      if (queuedCues.length > MAX_QUEUED_CUES) {
        queuedCues = queuedCues.slice(queuedCues.length - MAX_QUEUED_CUES);
      }
      return { ...state, ...band, queuedCues };
    }
    case "cueTick": {
      // Reconcile the band to wall-clock time (XERK-159): release every cue whose
      // window has closed and re-seat or clear the band. A no-op (same reference)
      // when the active cue is still inside its window, so a stale/early tick
      // can't churn the state.
      const band = advanceCues(state, action.now);
      if (band.activeCue === state.activeCue && band.queuedCues === state.queuedCues) return state;
      return { ...state, ...band };
    }
    case "error":
      return { ...state, error: action.message };
    case "togglePause":
      // Pausing clears the in-flight hypothesis so a stale partial doesn't linger.
      return { ...state, listening: !state.listening, partial: "" };
    case "micSwitch":
      return { ...state, micSource: action.micSource };
    case "stop":
      // Session over: drop to idle but keep the transcript on screen to read back
      // — including the past cues now embedded in it (XERK-108). Only the live
      // band's cues (active + queued) are ephemeral, so clear those and the schedule.
      return {
        ...state,
        running: false,
        listening: true,
        connection: "closed",
        partial: "",
        activeCue: null,
        queuedCues: [],
        activeCueEndsAt: null,
      };
  }
}

/**
 * One row of the live transcript: a finalized turn, or a past cue reviewed
 * inline (XERK-108). The mirror of history's segment/cue timeline, so the three
 * frontends interleave the two identically.
 */
export type LiveTranscriptItem =
  | { kind: "segment"; segment: CaptureSegment }
  | { kind: "cue"; cue: LiveCue };

/**
 * Merge finalized turns and past cues into one ordered transcript (XERK-108),
 * placing each cue right after the turn it was anchored to. A cue whose anchor
 * is `null` — or a turn since scrolled off the bounded segment window — leads the
 * transcript, before the first surviving turn. Cue order (release order) is
 * preserved within each anchor. Purely derived, so every frontend renders the
 * same interleaving and it's unit-tested in one place.
 */
export function liveTranscript(segments: CaptureSegment[], pastCues: LiveCue[]): LiveTranscriptItem[] {
  const present = new Set(segments.map((s) => s.id));
  const byAnchor = new Map<string, LiveCue[]>();
  const leading: LiveCue[] = [];
  for (const cue of pastCues) {
    const anchor = cue.afterSegmentId;
    if (anchor != null && present.has(anchor)) {
      const list = byAnchor.get(anchor);
      if (list) list.push(cue);
      else byAnchor.set(anchor, [cue]);
    } else {
      leading.push(cue);
    }
  }
  const items: LiveTranscriptItem[] = leading.map((cue) => ({ kind: "cue", cue }));
  for (const segment of segments) {
    items.push({ kind: "segment", segment });
    for (const cue of byAnchor.get(segment.id) ?? []) items.push({ kind: "cue", cue });
  }
  return items;
}

/**
 * Whole seconds left before a live cue auto-dismisses (XERK-110), given how
 * long it has been on screen. `Math.ceil` so the count reads the way a person
 * would say it: it shows 10 the instant the cue lands, 1 through the final
 * second, and 0 only once the cue is actually gone. Clamped at both ends so a
 * clock jump (or a timer that fires late) can't paint a number outside the
 * range the cue ever had.
 *
 * Pure and shared, so the lens box and the web/mobile cards count identically.
 */
export function cueSecondsLeft(elapsedMs: number, ttlMs: number = CUE_TTL_MS): number {
  const remaining = Math.ceil((ttlMs - elapsedMs) / 1000);
  return Math.max(0, Math.min(Math.ceil(ttlMs / 1000), remaining));
}

/**
 * Whole seconds left for the active cue, from the wall-clock time its window
 * closes (`endsAt`) and the current time `now` (XERK-159). Because it reads off
 * the real clock rather than counting local ticks, the count is truthful the
 * instant the app returns from the background — and a cue promoted mid-window
 * (its turn opened while you were away) shows the time it actually has left, not
 * a fresh ten. Delegates to `cueSecondsLeft` for the shared clamping.
 */
export function cueSecondsUntil(endsAt: number, now: number): number {
  return cueSecondsLeft(CUE_TTL_MS - (endsAt - now));
}

/** The countdown as it is painted in a cue's top-right corner, e.g. `"7s"`. */
export function cueCountdownLabel(secondsLeft: number): string {
  return `${secondsLeft}s`;
}

/** Minimal slice of `ApiClient` the session drives (so tests can inject a fake). */
export interface ApiLike {
  start(
    params: { micSource: MicSource; sourceLang?: Lang },
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
  // shows at a time, so only one timer is ever pending. `cueTimerFor` is the
  // wall-clock end time it targets (XERK-159), so a promoted cue re-arms for its
  // own window, a redundant sync is a no-op, and stop() can cancel it.
  private cueTimer: ReturnType<typeof setTimeout> | null = null;
  private cueTimerFor: number | null = null;

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
      onFinal: (m) =>
        this.dispatch({ type: "final", segmentId: m.segmentId, text: m.text, lang: m.lang }),
      onTranslation: (m) =>
        this.dispatch({ type: "translation", segmentId: m.segmentId, text: m.text }),
      onCue: (m) => this.showCue({ id: m.cueId, title: m.title, body: m.body, source: m.source }),
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

  /**
   * Show a live cue (XERK-81). It takes the band if nothing is up, else waits its
   * turn behind the active cue (XERK-102). The arrival first advances the schedule
   * to now, so any cue whose turn already elapsed (e.g. while backgrounded) is
   * retired into the transcript first (XERK-159); `syncCueTimer` then arms the
   * release timer for whichever cue now holds the band.
   */
  private showCue(cue: LiveCue): void {
    this.dispatch({ type: "cue", cue, now: Date.now() });
    this.syncCueTimer();
  }

  /**
   * Reconcile the cue schedule to real time and re-arm the release timer for the
   * remaining life of whatever cue now holds the band (XERK-159). Public so the
   * platform bindings can call it when the app returns to the foreground: the
   * release timer is unreliable while the app is backgrounded, so on return this
   * drains every cue whose turn passed while away in one pass instead of letting
   * them replay one-at-a-time.
   */
  syncCues(): void {
    this.dispatch({ type: "cueTick", now: Date.now() });
    this.syncCueTimer();
  }

  /**
   * Ensure exactly one release timer is pending, targeting the wall-clock end of
   * the active cue's window (XERK-159). Called after every cue transition: a
   * newly-seated cue arms its timer for the time it has left, a redundant call is
   * a no-op, and an empty band clears it. When the timer fires it reconciles the
   * schedule (releasing the cue and promoting the next) and re-syncs.
   */
  private syncCueTimer(): void {
    const endsAt = this.state.activeCueEndsAt;
    if (endsAt == null) {
      this.clearCueTimer();
      return;
    }
    if (this.cueTimerFor === endsAt) return; // already timing this window
    this.clearCueTimer();
    this.cueTimerFor = endsAt;
    this.cueTimer = setTimeout(
      () => {
        this.cueTimer = null;
        this.cueTimerFor = null;
        this.dispatch({ type: "cueTick", now: Date.now() });
        this.syncCueTimer();
      },
      Math.max(0, endsAt - Date.now()),
    );
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
