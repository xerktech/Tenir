/**
 * Resume session-id store for web live capture.
 *
 * `CaptureSession` persists the authoritative session id so a dropped socket (or a
 * page reload) resumes the same diarization session (master plan §4.2). On the web
 * that store is the browser's localStorage; the mobile app uses device secure storage.
 */

const KEY = "tenir.capture.sessionId";

export function loadSessionId(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function saveSessionId(id: string): void {
  try {
    window.localStorage.setItem(KEY, id);
  } catch {
    /* localStorage unavailable (private mode) — resume is best-effort. */
  }
}

export function clearSessionId(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}
