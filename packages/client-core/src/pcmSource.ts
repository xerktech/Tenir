/**
 * The microphone seam shared by every capture client (master plan §4.1, §10).
 *
 * The Even G2 app gets PCM pushed by the glasses host; the mobile app owns a native
 * recorder; the web SPA captures the browser mic. All three deliver the same 16 kHz
 * s16le mono PCM — the exact format the api STT wants, so nothing is resampled on the
 * wire. This interface is the platform-agnostic contract for that source, free of any
 * React Native or browser import so the capture state machine can be unit-tested with
 * a fake one.
 */

/** A live microphone source delivering 16 kHz s16le mono PCM. */
export interface PcmAudioSource {
  /**
   * Ask for microphone permission, prompting on first use. Resolves true once
   * recording is permitted. Cheap to call repeatedly (a no-op once granted).
   */
  requestPermission(): Promise<boolean>;

  /**
   * Why the most recent `requestPermission()` resolved false, when the source can be
   * more specific than a flat denial (insecure origin, no device, mic busy, …). Read
   * by the capture session to show the user something actionable; sources that can't
   * distinguish causes leave it unset and the session falls back to a generic message.
   */
  readonly lastPermissionError?: string;

  /**
   * Begin capturing. `onChunk` receives base64-encoded PCM in ~100 ms slices for the
   * life of the stream. Resolves true once the mic is actually running. Calling twice
   * without an intervening `stop()` is a no-op that keeps the existing stream.
   */
  start(onChunk: (base64Pcm: string) => void): Promise<boolean>;

  /** Stop capturing and release the microphone. Safe to call when already stopped. */
  stop(): Promise<void>;
}
