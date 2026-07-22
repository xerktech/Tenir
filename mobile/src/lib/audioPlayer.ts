/**
 * Pure playback state machine for the in-app audio player (XERK-67), kept
 * React-Native-free so it's unit-tested directly like `format.ts` / `controllers.ts`.
 *
 * The native `AudioPlayer` module owns the actual `MediaPlayer`; the hook
 * (`useAudioPlayer`) drives it and feeds *events* — a load result, periodic
 * position ticks, completion, errors — into this reducer, which is the single
 * source of truth the seek-bar UI renders. Keeping the transitions here (rather
 * than inline in the hook) is what lets us test the tricky bits — deriving
 * playing/paused/ended from a status tick, clamping a scrubbed seek — without a
 * device.
 */

export type PlaybackStatus =
  | "idle" // nothing loaded yet
  | "loading" // asked the native player to prepare the URL
  | "ready" // prepared, not yet played
  | "playing"
  | "paused"
  | "ended" // reached the end of the clip
  | "error";

export interface PlayerState {
  status: PlaybackStatus;
  /** Current playhead offset in milliseconds. */
  positionMs: number;
  /** Total clip length in milliseconds (0 until known). */
  durationMs: number;
  /** User-facing message when `status === "error"`, else null. */
  error: string | null;
}

export const initialPlayerState: PlayerState = {
  status: "idle",
  positionMs: 0,
  durationMs: 0,
  error: null,
};

export type PlayerEvent =
  /** Begin preparing a (new) clip — resets position/duration/error. */
  | { type: "load" }
  /** Native player finished preparing; the clip length is now known. */
  | { type: "loaded"; durationMs: number }
  /** Optimistic UI transitions issued alongside the native play()/pause() calls. */
  | { type: "play" }
  | { type: "pause" }
  /** A periodic position tick from the native player while playing/seeking. */
  | { type: "tick"; positionMs: number; durationMs: number; playing: boolean; ended: boolean }
  /** Optimistic scrub — moves the playhead ahead of the next native tick. */
  | { type: "seek"; positionMs: number }
  | { type: "error"; message: string }
  /** Tear down (unmount / URL change): back to idle. */
  | { type: "reset" };

/** Clamp `n` into the inclusive `[lo, hi]` range. */
function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

export function playerReducer(state: PlayerState, event: PlayerEvent): PlayerState {
  switch (event.type) {
    case "load":
      return { status: "loading", positionMs: 0, durationMs: 0, error: null };

    case "loaded":
      return { ...state, status: "ready", durationMs: Math.max(0, event.durationMs), positionMs: 0, error: null };

    case "play":
      // Ignore until something is loaded; from ended, replay from the top.
      if (state.status === "idle" || state.status === "loading" || state.status === "error") return state;
      return { ...state, status: "playing", positionMs: state.status === "ended" ? 0 : state.positionMs };

    case "pause":
      return state.status === "playing" ? { ...state, status: "paused" } : state;

    case "tick": {
      // The native tick is authoritative for position + playing/ended; keep the
      // longest duration we've seen (some players report 0 on the first tick).
      const durationMs = event.durationMs > 0 ? event.durationMs : state.durationMs;
      const positionMs = clamp(event.positionMs, 0, durationMs || event.positionMs);
      if (event.ended) return { ...state, status: "ended", durationMs, positionMs: durationMs };
      if (state.status === "error" || state.status === "idle") return state;
      return { ...state, status: event.playing ? "playing" : "paused", durationMs, positionMs };
    }

    case "seek": {
      const positionMs = clamp(event.positionMs, 0, state.durationMs || event.positionMs);
      // Scrubbing off the end of a finished clip re-arms it as paused at that point.
      const status = state.status === "ended" ? "paused" : state.status;
      return { ...state, status, positionMs };
    }

    case "error":
      return { ...state, status: "error", error: event.message };

    case "reset":
      return initialPlayerState;
  }
}

/** Playback progress as a 0..1 fraction for the seek-bar fill (0 when length unknown). */
export function progressFraction(state: PlayerState): number {
  if (state.durationMs <= 0) return 0;
  return clamp(state.positionMs / state.durationMs, 0, 1);
}

/** Map a 0..1 seek-bar position to a clamped millisecond offset to seek to. */
export function seekTargetMs(fraction: number, durationMs: number): number {
  if (durationMs <= 0) return 0;
  return Math.round(clamp(fraction, 0, 1) * durationMs);
}
