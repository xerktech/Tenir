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

import {
  browserOidcPrimitives,
  configureApi,
  configureOidc,
  configureOidcStore,
  configureTokenStore,
  httpBaseFromWs,
  type OidcStore,
  type TokenStore,
} from "@tenir/client-core";
import type { Lang, MicSource } from "@tenir/contract";

import { loadServerUrl, normalizeWsUrl, resolveWsUrl, saveServerUrl } from "./state/settings";
import type { KeyValueStorage } from "./state/storage";

const SEED_WS = import.meta.env.VITE_API_WS as string | undefined;
const HTTP_OVERRIDE = import.meta.env.VITE_API_HTTP as string | undefined;

/** Where the bearer token persists (same key client-core's default store used). */
export const TOKEN_KEY = "tenir.token";

/**
 * OIDC sidecar keys, mirroring client-core's private `auth.ts` constants (same
 * re-declared-string pattern as `TOKEN_KEY` above). Only the session sidecar
 * (refresh token + expiry) needs to outlive an app restart; the short-lived PKCE
 * transaction lives only for the authorize redirect, so it is not pre-loaded.
 */
export const OIDC_SESSION_KEY = "tenir.oidc.session";

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
 * An `OidcStore` for the optional Authentik path (XERK-656). The access token
 * rides the `TokenStore` above; this holds the OIDC *sidecar* — the refresh token
 * + expiry, and the PKCE transaction that spans the authorize redirect.
 *
 * It writes `localStorage` synchronously (client-core reads the sidecar
 * synchronously, and `localStorage` survives the authorize redirect within a run)
 * AND write-throughs to the device store, so the refresh token outlives an app
 * restart — which even's `localStorage` does NOT (see settings.ts). At init the
 * caller seeds it from the device snapshot, restoring the session after a restart
 * even though `localStorage` was wiped.
 */
export function deviceOidcStore(store: KeyValueStorage, snapshot: Record<string, string>): OidcStore {
  const mem = new Map<string, string>(Object.entries(snapshot));
  for (const [k, v] of mem) {
    try {
      localStorage.setItem(k, v);
    } catch {
      /* localStorage unavailable — reads fall back to the in-memory seed below */
    }
  }
  return {
    get(key) {
      try {
        const v = localStorage.getItem(key);
        if (v !== null) return v;
      } catch {
        /* fall back to the in-memory seed */
      }
      return mem.get(key) ?? null;
    },
    set(key, value) {
      mem.set(key, value);
      try {
        localStorage.setItem(key, value);
      } catch {
        /* just won't survive the redirect if localStorage is unavailable */
      }
      void store.set(key, value);
    },
    remove(key) {
      mem.delete(key);
      try {
        localStorage.removeItem(key);
      } catch {
        /* ignore */
      }
      void store.remove(key);
    },
  };
}

/**
 * The app's OIDC redirect URI — this page itself (origin + path, sans any
 * query/hash), which is where Authentik sends the browser back with the code.
 * It must match the redirect URI registered on the Authentik provider (T7).
 */
export function oidcRedirectUri(): string {
  const { origin, pathname } = window.location;
  return `${origin}${pathname}`;
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

  // Optional OIDC path (XERK-656): wire the sidecar store (seeded from the device
  // store so a restart keeps the session) and the browser PKCE primitives. With
  // OIDC off this is inert — the built-in path never touches it.
  const snapshot: Record<string, string> = {};
  const persistedSession = await store.get(OIDC_SESSION_KEY);
  if (persistedSession) snapshot[OIDC_SESSION_KEY] = persistedSession;
  configureOidcStore(deviceOidcStore(store, snapshot));
  configureOidc(browserOidcPrimitives(oidcRedirectUri()));
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
