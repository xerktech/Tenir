/**
 * Web SPA api configuration.
 *
 * The SPA is built into the api container image and served by the api itself, so
 * in production the api is simply the page's own origin. `VITE_API_HTTP` still
 * seeds a different api URL for local dev (Vite dev server on :5174 talking to an
 * api elsewhere). The resolved URL is pushed into the shared REST client via
 * `configureApi`.
 */

import { configureApi, setToken } from "@tenir/client-core";

const DEFAULT = "http://localhost:8080";

/**
 * Adopt a bearer token handed over in the URL fragment (`#token=…`) — the Even
 * G2 phone page embeds this web UI after signing in on the glasses side and
 * passes its token this way, so the embedded UI boots already signed in
 * (XERK-82). A fragment never reaches the server or its logs; it is stripped
 * from the address bar immediately after adoption. No-op when absent.
 */
export function adoptTokenFromUrl(win: Pick<Window, "location" | "history"> | undefined = typeof window !== "undefined" ? window : undefined): void {
  if (!win) return;
  const match = /[#&]token=([^&]+)/.exec(win.location.hash);
  if (!match) return;
  setToken(decodeURIComponent(match[1]));
  win.history.replaceState(null, "", win.location.pathname + win.location.search);
}

/**
 * Resolve the api URL: the dev-time `VITE_API_HTTP` seed takes precedence, then
 * the page's own origin (the api serves the SPA), then localhost outside a
 * browser (tests).
 */
export function getServerUrl(): string {
  const seed = (import.meta.env.VITE_API_HTTP as string | undefined)?.trim();
  if (seed) return seed.replace(/\/$/, "");
  if (typeof window !== "undefined" && /^https?:$/.test(window.location.protocol)) {
    return window.location.origin;
  }
  return DEFAULT;
}

// Point the shared REST client at the configured api at startup, and pick up a
// token handed over by the Even G2 phone page (before the app's first `me()`).
configureApi({ httpBaseUrl: getServerUrl() });
adoptTokenFromUrl();
