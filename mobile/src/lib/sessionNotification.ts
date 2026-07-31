/**
 * Content for the Android background-session notification (XERK-163).
 *
 * While a live session runs, the microphone foreground service posts an ongoing
 * notification (see `android/.../MicForegroundService.kt`). This pure helper decides what
 * that notification should say for a given session state so the decision is unit-tested
 * without touching the native module: it mirrors the active cue so a cue surfaces in the
 * notification shade while the app is backgrounded, and falls back to a plain "capturing"
 * line between cues. Kept free of `react-native` imports so it runs under vitest; the
 * thin native call lives in `../native/notification`.
 */

import type { LiveCue } from "@tenir/client-core";

export interface SessionNotification {
  title: string;
  text: string;
}

/** Default title when no cue fills the notification — the app/brand name. */
export const CAPTURING_TITLE = "Tenir";

/** Collapsed line shown when a session is live but no cue is currently in the band. */
export const CAPTURING_TEXT = "Live session · capturing audio";

/**
 * The content the background notification should show for the current session, or `null`
 * when no session is running — in which case the caller leaves the notification alone
 * (the foreground service and its notification are torn down by `stop()`).
 */
export function sessionNotificationContent(
  running: boolean,
  cue: LiveCue | null,
): SessionNotification | null {
  if (!running) return null;
  if (cue) return { title: cue.title, text: cue.body };
  return { title: CAPTURING_TITLE, text: CAPTURING_TEXT };
}
