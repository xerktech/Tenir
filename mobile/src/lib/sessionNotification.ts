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
  /**
   * Wall-clock epoch ms when a cue's on-screen window closes, so the native side can
   * revert the notification to the capturing line on time (XERK-163). This must be done
   * natively: the JS cue-release timer is frozen while the app is backgrounded — exactly
   * when the notification is in use — so a cue would otherwise stay pinned in the shade
   * until the app next returns to the foreground. `0` means no auto-expiry: the capturing
   * fallback stays until a cue or `stop()` supersedes it.
   */
  endsAt: number;
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
  activeCueEndsAt: number | null,
): SessionNotification | null {
  if (!running) return null;
  if (cue) return { title: cue.title, text: cue.body, endsAt: activeCueEndsAt ?? 0 };
  return { title: CAPTURING_TITLE, text: CAPTURING_TEXT, endsAt: 0 };
}
