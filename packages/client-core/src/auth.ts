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
 */

const TOKEN_KEY = "tenir.token";

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
