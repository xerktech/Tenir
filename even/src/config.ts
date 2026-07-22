/**
 * Even Hub app configuration.
 *
 * The api URL is a REQUIRED, user-editable setting (master plan §8.5): the
 * wearer points the app at their own self-hosted instance on the phone login
 * page, and the lens reads the same persisted choice at boot. `VITE_API_WS` is
 * only a build-time *seed* for dev / first run — a saved choice always wins.
 *
 * XERK-82: settings and the bearer token persist through `KeyValueStorage`
 * (bridge-backed on device — browser `localStorage` does not survive app
 * restarts in this host), so `initConfig` must run before anything talks to the
 * api: it loads the saved URL + token and wires both into `@tenir/client-core`.
 * The token store mirrors in memory (client-core needs synchronous reads) and
 * write-throughs to the device store in the background.
 */

import { configureApi, configureTokenStore, httpBaseFromWs, type TokenStore } from "@tenir/client-core";
import type { Lang, MicSource } from "@tenir/contract";

import { loadServerUrl, normalizeWsUrl, resolveWsUrl, saveServerUrl } from "./state/settings";
import type { KeyValueStorage } from "./state/storage";

const SEED_WS = import.meta.env.VITE_API_WS as string | undefined;
const HTTP_OVERRIDE = import.meta.env.VITE_API_HTTP as string | undefined;

/** Where the bearer token persists (same key client-core's default store used). */
export const TOKEN_KEY = "tenir.token";

let storage: KeyValueStorage | null = null;
let saved = false; // whether a user-chosen server URL is persisted
let currentWsUrl = resolveWsUrl(null, SEED_WS);
let currentHttpUrl = HTTP_OVERRIDE ?? httpBaseFromWs(currentWsUrl);

/**
 * A `TokenStore` mirroring the token in memory (synchronous for client-core) and
 * persisting writes to the device store in the background (master plan §8.5's
 * native-store pattern). Exported for tests.
 */
export function deviceTokenStore(store: KeyValueStorage, initial: string | null): TokenStore {
  let current = initial;
  return {
    get: () => current,
    set: (token) => {
      current = token;
      void store.set(TOKEN_KEY, token);
    },
    clear: () => {
      current = null;
      void store.remove(TOKEN_KEY);
    },
  };
}

/**
 * Load the persisted server URL + bearer token and point `client-core` at them.
 * Must complete before any REST/WS use; called once from `main.ts` with the
 * device-appropriate storage.
 */
export async function initConfig(store: KeyValueStorage): Promise<void> {
  storage = store;
  const persisted = await loadServerUrl(store);
  saved = persisted !== null;
  currentWsUrl = resolveWsUrl(persisted, SEED_WS);
  // A saved choice drives both URLs; the explicit VITE_API_HTTP override only applies
  // while running on the seed (dev proxying), so a user-chosen server stays consistent.
  currentHttpUrl = saved ? httpBaseFromWs(currentWsUrl) : (HTTP_OVERRIDE ?? httpBaseFromWs(currentWsUrl));
  configureApi({ httpBaseUrl: currentHttpUrl });
  configureTokenStore(deviceTokenStore(store, await store.get(TOKEN_KEY)));
}

export const config = {
  /** Effective api WebSocket endpoint (saved choice → build seed → localhost). */
  get apiWsUrl(): string {
    return currentWsUrl;
  },
  /** Effective api REST base, used by the phone page (sign-in, embedded web UI). */
  get apiHttpUrl(): string {
    return currentHttpUrl;
  },
  /** Default microphone (glasses by default, phone for seated/table). */
  defaultMicSource: "g2-microphone" as MicSource,
  /** Leave the source language unset so STT auto-detects per turn. */
  defaultSourceLang: undefined as Lang | undefined,
} as const;

/** Whether the user has explicitly configured a server URL (vs. running on the seed). */
export function isServerConfigured(): boolean {
  return saved;
}

/**
 * Persist + apply a user-entered server URL (already normalized to ws(s) form),
 * repointing the shared REST client. Returns the resolved ws/http pair, or null
 * when the input isn't a valid ws(s) URL (so the caller can reject it).
 */
export async function applyServerUrl(
  rawWsUrl: string,
): Promise<{ wsUrl: string; httpBaseUrl: string } | null> {
  const wsUrl = normalizeWsUrl(rawWsUrl);
  if (!wsUrl) return null;
  if (storage) await saveServerUrl(storage, wsUrl);
  saved = true;
  currentWsUrl = wsUrl;
  currentHttpUrl = httpBaseFromWs(wsUrl);
  configureApi({ httpBaseUrl: currentHttpUrl });
  return { wsUrl, httpBaseUrl: currentHttpUrl };
}
