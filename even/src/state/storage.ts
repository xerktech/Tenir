/**
 * Hardware-agnostic key/value persistence for the Even Hub app.
 *
 * Browser `localStorage`/IndexedDB do NOT survive the headless-WebView migration
 * in this host (see `persist.ts`) — the SDK's `setLocalStorage`/`getLocalStorage`
 * are the only store that persists across app restarts on real glasses. Settings
 * that must survive a relaunch (server URL, sign-in) therefore go through this
 * interface: `BridgeStorage` on the device path, `BrowserStorage` for plain-browser
 * dev where no bridge exists (and window.localStorage does persist).
 */

export interface KeyValueStorage {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
  /** Forget a key. Implemented as an empty write where the SDK has no remove. */
  remove(key: string): Promise<void>;
}

/**
 * Cap on any single bridge (BLE) round-trip. The Even docs warn a flaky hop can
 * hang ~30s — an unbounded await before the lens draws makes the whole app look
 * dead, so every bridge call races this and falls back instead (XERK-82).
 */
export const BLE_TIMEOUT_MS = 4000;

/** Race a bridge call against BLE_TIMEOUT_MS, resolving `fallback` on timeout. */
export function withBleTimeout<T>(call: Promise<T>, fallback: T, ms: number = BLE_TIMEOUT_MS): Promise<T> {
  return Promise.race([
    call,
    new Promise<T>((resolve) => setTimeout(() => resolve(fallback), ms)),
  ]);
}

/** Dev/browser implementation backed by `window.localStorage`. */
export class BrowserStorage implements KeyValueStorage {
  async get(key: string): Promise<string | null> {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  async set(key: string, value: string): Promise<void> {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* storage unavailable — the value just won't persist */
    }
  }

  async remove(key: string): Promise<void> {
    try {
      window.localStorage.removeItem(key);
    } catch {
      /* ignore */
    }
  }
}

/**
 * Structural stand-in for the slice of `EvenAppBridge` this class calls — no SDK
 * import needed here (a real bridge satisfies this shape), so tests can pass a
 * plain object.
 */
export interface StorageBridge {
  getLocalStorage(key: string): Promise<string>;
  setLocalStorage(key: string, value: string): Promise<boolean>;
}

/**
 * Bridge-backed implementation for the real Even Hub hardware path. Writes share
 * the BLE link, so callers should keep values small; failures and hangs are
 * bounded (a dropped write just means the value doesn't persist this once, a
 * hung read resolves as a miss after BLE_TIMEOUT_MS instead of stalling boot).
 */
export class BridgeStorage implements KeyValueStorage {
  constructor(private readonly bridge: StorageBridge) {}

  async get(key: string): Promise<string | null> {
    try {
      const raw = await withBleTimeout(this.bridge.getLocalStorage(key), "");
      // getLocalStorage resolves "" when the key doesn't exist (per the SDK
      // reference) — treat that as a miss, same as BrowserStorage's null.
      return raw ? raw : null;
    } catch (err) {
      console.error("tenir: getLocalStorage failed:", err);
      return null;
    }
  }

  async set(key: string, value: string): Promise<void> {
    try {
      await withBleTimeout(this.bridge.setLocalStorage(key, value), false);
    } catch (err) {
      console.error("tenir: setLocalStorage failed:", err);
    }
  }

  async remove(key: string): Promise<void> {
    // The SDK has no remove — an empty write reads back as a miss (see get()).
    await this.set(key, "");
  }
}
