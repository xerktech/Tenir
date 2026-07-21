/**
 * Session persistence across background/foreground (master plan §4.1).
 *
 * Browser localStorage/IndexedDB do NOT survive the headless-WebView migration in
 * this host; the SDK's `setLocalStorage`/`getLocalStorage` are the only reliable
 * store. (SDK 0.0.10 does not export the `setBackgroundState` helper, so we persist
 * explicitly here.) Writes share the BLE link, so they're debounced and flushed on
 * `FOREGROUND_EXIT`.
 */

import type { EvenAppBridge } from "@evenrealities/even_hub_sdk";

import type { MicSource } from "@tenir/contract";

const KEY = "tenir.session";
const DEBOUNCE_MS = 1500;

export interface PersistedSession {
  sessionId?: string;
  micSource: MicSource;
  transcript: string; // rolling caption text, trimmed to the lens window
}

export class SessionStore {
  private bridge: EvenAppBridge;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pending: PersistedSession | null = null;

  constructor(bridge: EvenAppBridge) {
    this.bridge = bridge;
  }

  async load(): Promise<PersistedSession | null> {
    try {
      const raw = await this.bridge.getLocalStorage(KEY);
      if (!raw) return null;
      return JSON.parse(raw) as PersistedSession;
    } catch {
      return null;
    }
  }

  /** Debounced save — coalesces frequent transcript updates into one BLE write. */
  save(state: PersistedSession): void {
    this.pending = state;
    if (this.timer) return;
    this.timer = setTimeout(() => void this.flush(), DEBOUNCE_MS);
  }

  /** Force-write immediately (call on FOREGROUND_EXIT). */
  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (!this.pending) return;
    const state = this.pending;
    this.pending = null;
    try {
      await this.bridge.setLocalStorage(KEY, JSON.stringify(state));
    } catch {
      // best-effort; a dropped write just means we resume from an older snapshot
    }
  }

  async clear(): Promise<void> {
    this.pending = null;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    try {
      await this.bridge.setLocalStorage(KEY, "");
    } catch {
      /* ignore */
    }
  }
}
