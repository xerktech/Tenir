/**
 * Persistent key/value storage + the secure token-store mirror (master plan §8.5).
 *
 * The mobile app can't use `localStorage` (it doesn't exist in React Native), so it
 * injects its own bearer-token store into `client-core` via `configureTokenStore`.
 * On device that store is backed by the OS keychain/keystore (see `secureStorage.ts`),
 * which is **async** — but `client-core`'s `getToken`/`authHeader` are synchronous and
 * called on every request. `createMirroredTokenStore` bridges that: it keeps the token
 * in memory for synchronous reads, hydrates it once at startup, and writes through to
 * the async backing store in the background.
 *
 * This module is deliberately React-Native-agnostic (it takes a `KeyValueStore` rather
 * than importing a native module) so it can be unit-tested with an in-memory store.
 */

import type { TokenStore } from "@tenir/client-core";

/** A minimal async key/value store — the shape of `@react-native-async-storage`. */
export interface KeyValueStore {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

/** An in-memory `KeyValueStore` — the test/fallback backing when no keystore is present. */
export function memoryKeyValue(initial: Record<string, string> = {}): KeyValueStore {
  const map = new Map<string, string>(Object.entries(initial));
  return {
    getItem: (k) => Promise.resolve(map.get(k) ?? null),
    setItem: (k, v) => {
      map.set(k, v);
      return Promise.resolve();
    },
    removeItem: (k) => {
      map.delete(k);
      return Promise.resolve();
    },
  };
}

const TOKEN_KEY = "tenir.token";

export interface MirroredTokenStore {
  /** The synchronous store handed to `client-core` via `configureTokenStore`. */
  store: TokenStore;
  /** Read the persisted token into memory once at startup; call before rendering. */
  hydrate(): Promise<void>;
}

/**
 * Build a synchronous `TokenStore` mirrored over an async `KeyValueStore`. Reads come
 * from the in-memory mirror; writes update memory immediately and persist in the
 * background. `onError` (optional) surfaces background persistence failures.
 */
export function createMirroredTokenStore(
  kv: KeyValueStore,
  onError?: (err: unknown) => void,
): MirroredTokenStore {
  let cached: string | null = null;
  const swallow = (err: unknown) => onError?.(err);

  return {
    store: {
      get: () => cached,
      set: (token) => {
        cached = token;
        kv.setItem(TOKEN_KEY, token).catch(swallow);
      },
      clear: () => {
        cached = null;
        kv.removeItem(TOKEN_KEY).catch(swallow);
      },
    },
    async hydrate() {
      try {
        cached = await kv.getItem(TOKEN_KEY);
      } catch (err) {
        swallow(err);
        cached = null;
      }
    },
  };
}

const SERVER_URL_KEY = "tenir.serverUrl";

/** Load the persisted api WS URL, or null if the user hasn't chosen one yet. */
export function loadServerUrl(kv: KeyValueStore): Promise<string | null> {
  return kv.getItem(SERVER_URL_KEY).catch(() => null);
}

/** Persist the chosen api WS URL (best-effort; ignores storage failures). */
export function saveServerUrl(kv: KeyValueStore, wsUrl: string): Promise<void> {
  return kv.setItem(SERVER_URL_KEY, wsUrl).catch(() => undefined);
}

const SESSION_ID_KEY = "tenir.sessionId";

/**
 * The capture session id, persisted per device (master plan §10: per-client session
 * ownership). On the next launch or after a drop the capture client resumes *this*
 * session so diarization continuity survives the gap; it is cleared when the user
 * ends the session.
 */
export function loadSessionId(kv: KeyValueStore): Promise<string | null> {
  return kv.getItem(SESSION_ID_KEY).catch(() => null);
}

/** Persist the authoritative capture session id (best-effort). */
export function saveSessionId(kv: KeyValueStore, sessionId: string): Promise<void> {
  return kv.setItem(SESSION_ID_KEY, sessionId).catch(() => undefined);
}

/** Forget the persisted capture session id when the session ends (best-effort). */
export function clearSessionId(kv: KeyValueStore): Promise<void> {
  return kv.removeItem(SESSION_ID_KEY).catch(() => undefined);
}
