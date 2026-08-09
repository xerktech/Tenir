/**
 * Web SPA api configuration.
 *
 * The SPA is built into the api container image and served by the api itself, so
 * in production the api is simply the page's own origin. `VITE_API_HTTP` still
 * seeds a different api URL for local dev (Vite dev server on :5174 talking to an
 * api elsewhere). The resolved URL is pushed into the shared REST client via
 * `configureApi`.
 */

import { configureApi, getToken, setToken } from "@tenir/client-core";

const DEFAULT = "http://localhost:8080";

/**
 * Adopt a bearer token handed over in the URL fragment (`#token=…`) — the Even
 * G2 phone page embeds this web UI after signing in on the glasses side and
 * passes its token this way, so the embedded UI boots already signed in
 * (XERK-82). A fragment never reaches the server or its logs; it is stripped
 * from the address bar immediately after adoption. No-op when absent.
 *
 * **Only ever adopted into a signed-OUT browser.** This used to overwrite
 * whatever token was already stored, so any link — a chat message, an image
 * `src`, a redirect — could silently swap the signed-in account: the victim's
 * own history vanished, every session they recorded afterwards landed in the
 * attacker's household, and the fragment was scrubbed from the address bar so
 * nothing looked wrong. `#token=garbage` destroyed a working session just as
 * quietly. Refusing to replace an existing session closes both (XERK-236).
 *
 * The handoff into a *fresh* browser is still an unauthenticated write — an
 * attacker link can log a stranger into the attacker's own account. Closing
 * that needs a one-time, server-issued handoff code rather than a raw token in
 * a URL; see qa.md.
 */
export function adoptTokenFromUrl(win: Pick<Window, "location" | "history"> | undefined = typeof window !== "undefined" ? window : undefined): void {
  if (!win) return;
  const match = /[#&]token=([^&]+)/.exec(win.location.hash);
  if (!match) return;
  // Always strip the fragment, adopted or not, so a token never lingers in the
  // address bar, in browser history, or in a screenshot.
  const strip = () => win.history.replaceState(null, "", win.location.pathname + win.location.search);
  if (getToken()) {
    strip();
    return;
  }
  setToken(decodeURIComponent(match[1]));
  strip();
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
