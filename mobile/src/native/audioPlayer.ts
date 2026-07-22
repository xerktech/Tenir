/**
 * Typed access to the `AudioPlayer` native module (XERK-67) — the Android half of
 * in-app playback of a retained conversation's audio, mirroring the `AppUpdater`
 * wrapper (`native/appUpdater.ts`). It wraps a single Android `MediaPlayer`:
 * prepare a URL, play/pause/seek, and stream position ticks back to JS so the
 * seek bar can track and scrub. The pure state machine lives in `lib/audioPlayer.ts`
 * and the UI glue in `lib/useAudioPlayer.ts` + `ui/AudioPlayer.tsx`.
 *
 * Android-only: the module is only registered in the Android app, so on iOS (and
 * under the vitest/jsdom test runner) `audioPlayerAvailable` is false and the
 * helpers reject — the History screen then falls back to opening the clip in the
 * system browser.
 */

import { NativeEventEmitter, NativeModules, Platform } from "react-native";

/** A position tick emitted on `AudioPlayer.tick` while a clip is loaded. */
export interface AudioTick {
  positionMs: number;
  durationMs: number;
  playing: boolean;
  ended: boolean;
}

interface AudioPlayerNative {
  /** Prepare [url] for playback; resolves the clip length in milliseconds. */
  load(url: string): Promise<number>;
  play(): Promise<void>;
  pause(): Promise<void>;
  /** Seek the playhead to [positionMs]. */
  seek(positionMs: number): Promise<void>;
  /** Stop and release the underlying player. */
  release(): Promise<void>;
}

const native: AudioPlayerNative | null =
  (NativeModules.AudioPlayer as AudioPlayerNative | undefined) ?? null;

/** True only where the native module is present (the Android app). */
export const audioPlayerAvailable = Platform.OS === "android" && native !== null;

/** Emits `AudioPlayer.tick` ({@link AudioTick}) while a clip is loaded; null when unavailable. */
export const audioPlayerEvents: NativeEventEmitter | null = native
  ? new NativeEventEmitter(NativeModules.AudioPlayer)
  : null;

function unavailable(): Promise<never> {
  return Promise.reject(new Error("AudioPlayer unavailable"));
}

/** Prepare [url]; resolves the clip length in milliseconds. */
export function load(url: string): Promise<number> {
  return native === null ? unavailable() : native.load(url);
}

export function play(): Promise<void> {
  return native === null ? unavailable() : native.play();
}

export function pause(): Promise<void> {
  return native === null ? unavailable() : native.pause();
}

/** Seek the playhead to [positionMs]. */
export function seek(positionMs: number): Promise<void> {
  return native === null ? unavailable() : native.seek(positionMs);
}

/** Stop and release the underlying player. */
export function release(): Promise<void> {
  return native === null ? unavailable() : native.release();
}
