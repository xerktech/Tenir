/**
 * `useAudioPlayer` (XERK-67) — drives in-app playback of a retained clip through
 * the native `AudioPlayer` module, exposing the reducer state plus the two actions
 * the seek bar needs: toggle play/pause, and scrub to a 0..1 position.
 *
 * On mount (or when [url] changes) it asks the native player to prepare the URL,
 * then subscribes to position ticks. The pure transitions live in
 * `lib/audioPlayer.ts`; this hook is the imperative shell around the native side.
 *
 * Where the native module is absent (iOS, tests) `audioPlayerAvailable` is false —
 * callers gate on it and fall back to opening the clip externally, so nothing here
 * runs off-device.
 */

import { useCallback, useEffect, useReducer } from "react";

import {
  initialPlayerState,
  playerReducer,
  seekTargetMs,
  type PlayerState,
} from "./audioPlayer";
import {
  audioPlayerAvailable,
  audioPlayerEvents,
  load,
  pause,
  play,
  release,
  seek,
  type AudioTick,
} from "../native/audioPlayer";

export interface AudioPlayerControls {
  state: PlayerState;
  /** True only where native playback is possible (the Android app). */
  available: boolean;
  /** Play when paused/ready/ended, pause when playing. */
  toggle: () => void;
  /** Scrub to a 0..1 position along the clip. */
  seekToFraction: (fraction: number) => void;
}

export function useAudioPlayer(url: string): AudioPlayerControls {
  const [state, dispatch] = useReducer(playerReducer, initialPlayerState);

  // Prepare the clip on mount / URL change; release it on the way out so a second
  // opened session doesn't leave the first one's MediaPlayer holding the audio.
  useEffect(() => {
    if (!audioPlayerAvailable) return;
    let live = true;
    dispatch({ type: "load" });
    load(url)
      .then((durationMs) => {
        if (live) dispatch({ type: "loaded", durationMs });
      })
      .catch((err: unknown) => {
        if (live) dispatch({ type: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      live = false;
      void release();
      dispatch({ type: "reset" });
    };
  }, [url]);

  // Position ticks from the native player keep the seek bar tracking playback.
  useEffect(() => {
    if (audioPlayerEvents === null) return;
    const sub = audioPlayerEvents.addListener("AudioPlayer.tick", (t: AudioTick) =>
      dispatch({ type: "tick", positionMs: t.positionMs, durationMs: t.durationMs, playing: t.playing, ended: t.ended }),
    );
    return () => sub.remove();
  }, []);

  const toggle = useCallback(() => {
    if (state.status === "playing") {
      dispatch({ type: "pause" });
      void pause();
    } else {
      dispatch({ type: "play" });
      // Replaying a finished clip starts from the top; rewind the native side too.
      if (state.status === "ended") void seek(0);
      void play();
    }
  }, [state.status]);

  const seekToFraction = useCallback(
    (fraction: number) => {
      const positionMs = seekTargetMs(fraction, state.durationMs);
      dispatch({ type: "seek", positionMs });
      void seek(positionMs);
    },
    [state.durationMs],
  );

  return { state, available: audioPlayerAvailable, toggle, seekToFraction };
}
