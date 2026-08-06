/**
 * Bearer-token storage for the authenticated household.
 *
 * Ported from Tenir's `packages/client-core/src/auth.ts`. Auth is always
 * required: every REST call needs an `Authorization: Bearer <token>` header and
 * the WS needs a `?token=` query param (the WebSocket API can't set headers).
 *
 * Upstream's default store was `localStorage`; the miniapp background JSContext
 * has a polyfilled `localStorage` but the durable store is `session.storage`
 * (async), so the default here is a plain in-memory store and the controller
 * injects a store via `configureTokenStore` that mirrors the token in memory
 * and persists writes to `session.storage` in the background — keeping
 * `getToken`/`authHeader`/`withToken` synchronous for callers, exactly the
 * native-store pattern the upstream mobile app used.
 */

/**
 * A synchronous bearer-token store. `getToken`/`authHeader`/`withToken` are
 * called on every request and must stay synchronous, so an async-backed store
 * mirrors the token in memory and persists writes in the background.
 */
export interface TokenStore {
  get(): string | null;
  set(token: string): void;
  clear(): void;
}

/** The default in-memory store (tests, and before the controller wires storage). */
function memoryTokenStore(): TokenStore {
  let current: string | null = null;
  return {
    get: () => current,
    set(token) {
      current = token;
    },
    clear() {
      current = null;
    },
  };
}

let store: TokenStore = memoryTokenStore();

/**
 * Replace the bearer-token store. The controller calls this once at startup
 * with a `session.storage`-backed store so tokens persist on device.
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
