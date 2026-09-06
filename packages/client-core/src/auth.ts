/**
 * Bearer-token storage for the authenticated household (master plan §7).
 *
 * Auth is always required: every REST call needs an `Authorization: Bearer <token>`
 * header and the WS needs a `?token=` query param (the browser WebSocket API can't
 * set headers). Login stores the token here; the lens app and the web SPA both read
 * it. Before login (or after logout) the token is simply absent and protected calls
 * 401, which the clients surface as the login screen.
 *
 * The default backing store is `localStorage`, which is available in every browser
 * frontend that consumes this core (the Even Hub WebView and the web SPA alike).
 * Native frontends without `localStorage` — the React Native mobile app — inject
 * their own **secure keychain/keystore** store via `configureTokenStore` (master
 * plan §8.5), keeping `getToken`/`authHeader`/`withToken` synchronous for callers.
 *
 * The access token itself is **shape-agnostic** (`docs/auth-oidc.md` §10): the store
 * holds an opaque string, whether that is a built-in HMAC token or an Authentik OIDC
 * access token, so REST/WS auth need no branch. The one thing the client must track
 * is *which kind* of session is active — an OIDC session silently refreshes via the
 * IdP, a built-in one rides the api's `X-Renewed-Token`. That sidecar lives in the
 * separate `OidcStore` below and is what `getSessionKind()` reports.
 */

const TOKEN_KEY = "tenir.token";
const OIDC_SESSION_KEY = "tenir.oidc.session";
const OIDC_TX_KEY = "tenir.oidc.tx";

/**
 * A synchronous bearer-token store. `getToken`/`authHeader`/`withToken` are called
 * on every request and must stay synchronous, so a native (async keychain) store
 * mirrors the token in memory and persists writes in the background.
 */
export interface TokenStore {
  get(): string | null;
  set(token: string): void;
  clear(): void;
}

/** The default `localStorage`-backed store, used by the browser frontends. */
function localStorageTokenStore(): TokenStore {
  return {
    get() {
      try {
        return localStorage.getItem(TOKEN_KEY);
      } catch {
        return null;
      }
    },
    set(token) {
      try {
        localStorage.setItem(TOKEN_KEY, token);
      } catch {
        /* storage unavailable — token just won't persist */
      }
    },
    clear() {
      try {
        localStorage.removeItem(TOKEN_KEY);
      } catch {
        /* ignore */
      }
    },
  };
}

let store: TokenStore = localStorageTokenStore();

/**
 * Replace the bearer-token store (master plan §8.5). The mobile app calls this once
 * at startup with a keychain/keystore-backed store so tokens persist securely on
 * device instead of in (non-existent) `localStorage`.
 */
export function configureTokenStore(custom: TokenStore): void {
  store = custom;
}

/** The stored bearer token, or null when not logged in yet. */
export function getToken(): string | null {
  return store.get();
}

export function setToken(token: string): void {
  store.set(token);
}

export function clearToken(): void {
  store.clear();
}

/** Authorization header for REST calls, or an empty object when there's no token. */
export function authHeader(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Append the bearer token to a WS URL as `?token=` so the api can authenticate it. */
export function withToken(wsUrl: string): string {
  const token = getToken();
  if (!token) return wsUrl;
  const sep = wsUrl.includes("?") ? "&" : "?";
  return `${wsUrl}${sep}token=${encodeURIComponent(token)}`;
}

// ---- OIDC session sidecar ---------------------------------------------------
//
// The access token lives in the `TokenStore` above (shape-agnostic). Everything an
// OIDC session needs *beyond* the access token — the refresh token and the access
// token's expiry, plus the short-lived PKCE transaction spanning the authorize
// redirect — lives here, in a small keyed key/value store the flow in `oidc.ts`
// drives. Its presence is also what distinguishes an OIDC session from a built-in
// one (`getSessionKind`).

/**
 * A keyed string store for the OIDC sidecar. Defaults to `localStorage`; a native
 * frontend injects a keychain-backed one via `configureOidcStore` so the (sensitive)
 * refresh token persists securely on device rather than in (non-existent) storage.
 */
export interface OidcStore {
  get(key: string): string | null;
  set(key: string, value: string): void;
  remove(key: string): void;
}

/** What an active OIDC session needs to silently refresh itself before expiry. */
export interface OidcSession {
  /** The IdP refresh token, exchanged for a fresh access token near expiry. */
  refreshToken: string | null;
  /** Access-token expiry as epoch ms, or null when the IdP didn't state one. */
  expiresAt: number | null;
}

/** The in-flight PKCE transaction, persisted across the authorize redirect. */
export interface OidcTransaction {
  verifier: string;
  state: string;
  nonce: string;
}

function localStorageOidcStore(): OidcStore {
  return {
    get(key) {
      try {
        return localStorage.getItem(key);
      } catch {
        return null;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, value);
      } catch {
        /* storage unavailable — just won't persist */
      }
    },
    remove(key) {
      try {
        localStorage.removeItem(key);
      } catch {
        /* ignore */
      }
    },
  };
}

let oidcStore: OidcStore = localStorageOidcStore();

/** Replace the OIDC sidecar store (mobile injects a keychain-backed one at startup). */
export function configureOidcStore(custom: OidcStore): void {
  oidcStore = custom;
}

function readJson<T>(key: string): T | null {
  const raw = oidcStore.get(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

/** The active OIDC session sidecar, or null for a built-in / logged-out session. */
export function getOidcSession(): OidcSession | null {
  return readJson<OidcSession>(OIDC_SESSION_KEY);
}

export function setOidcSession(session: OidcSession): void {
  oidcStore.set(OIDC_SESSION_KEY, JSON.stringify(session));
}

export function clearOidcSession(): void {
  oidcStore.remove(OIDC_SESSION_KEY);
}

/** The pending PKCE transaction saved at authorize time, or null if none is in flight. */
export function getOidcTransaction(): OidcTransaction | null {
  return readJson<OidcTransaction>(OIDC_TX_KEY);
}

export function setOidcTransaction(tx: OidcTransaction): void {
  oidcStore.set(OIDC_TX_KEY, JSON.stringify(tx));
}

export function clearOidcTransaction(): void {
  oidcStore.remove(OIDC_TX_KEY);
}

/** Which auth backend the stored token belongs to — an OIDC session iff a sidecar exists. */
export function getSessionKind(): "builtin" | "oidc" {
  return getOidcSession() ? "oidc" : "builtin";
}

// ---- OIDC refresh hook ------------------------------------------------------
//
// The REST client (`api.ts`) and the WS client (`ws.ts`) must, on a 401 / 1008,
// silently refresh an OIDC token and retry — but they can't import the flow in
// `oidc.ts` without a cycle (it imports `api.ts`). So `configureOidc` registers its
// refresher here, and the transports call the seam. With no OIDC configured (or a
// built-in session) the seam is a no-op that reports "not refreshed", so the
// built-in path is byte-for-byte unchanged.

let oidcRefresher: (() => Promise<boolean>) | null = null;

/** Register the OIDC silent-refresh callback (called by `configureOidc`). */
export function registerOidcRefresher(fn: (() => Promise<boolean>) | null): void {
  oidcRefresher = fn;
}

/**
 * Attempt a silent OIDC refresh. Resolves `true` only when a fresh access token was
 * obtained and stored; `false` for a built-in session, no OIDC configured, or a
 * failed refresh (the caller then surfaces re-login).
 */
export async function tryOidcRefresh(): Promise<boolean> {
  if (!oidcRefresher) return false;
  return oidcRefresher();
}
