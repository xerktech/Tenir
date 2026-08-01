/**
 * Native bridge for the Android background-session notification (XERK-163).
 *
 * Pushes the current session's content (from `sessionNotificationContent`) into the
 * microphone foreground service's ongoing notification via the `PcmAudio` native module,
 * so a live cue appears in the notification shade while the app is backgrounded.
 *
 * Android-only: iOS keeps capture alive under a background-audio entitlement with no
 * equivalent ongoing notification, and the native module is absent in JS-only runtimes —
 * both are silent no-ops. Like `audio/native.ts`, it imports `react-native` and so is
 * typechecked but not unit-tested; the decision it acts on is covered via
 * `sessionNotificationContent`.
 */

import { NativeModules, Platform } from "react-native";

import type { SessionNotification } from "../lib/sessionNotification";

interface PcmAudioNotification {
  updateNotification(title: string, text: string, endsAt: number): Promise<void>;
}

/**
 * Post `content` to the ongoing session notification. A `null` content (no session
 * running) is a no-op: the service and its notification are torn down by `stop()`, so
 * there is nothing to update. `content.endsAt` (a cue's wall-clock expiry, `0` for the
 * capturing fallback) lets the native service clear a cue on time even while JS timers
 * are frozen in the background.
 */
export function postSessionNotification(content: SessionNotification | null): void {
  if (Platform.OS !== "android" || content == null) return;
  const native = NativeModules.PcmAudio as Partial<PcmAudioNotification> | undefined;
  void native?.updateNotification?.(content.title, content.text, content.endsAt);
}
