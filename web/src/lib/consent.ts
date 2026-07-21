/**
 * One-time recording-notice acceptance for web live capture (master plan §9).
 *
 * Before the first capture the web UI shows the recording notice (the same copy the
 * mobile app surfaces); acceptance is remembered in localStorage so later sessions
 * skip it. This is a local UX gate, not a substitute for the server capture policy.
 */

const KEY = "tenir.capture.recordingNoticeAccepted";

export function recordingNoticeAccepted(): boolean {
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function acceptRecordingNotice(): void {
  try {
    window.localStorage.setItem(KEY, "1");
  } catch {
    /* ignore */
  }
}
