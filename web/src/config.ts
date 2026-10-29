/**
 * Web SPA api configuration.
 *
 * The SPA is built into the api container image and served by the api itself, so
 * in production the api is simply the page's own origin. `VITE_API_HTTP` still
 * seeds a different api URL for local dev (Vite dev server on :5174 talking to an
 * api elsewhere). The resolved URL is pushed into the shared REST client via
 * `configureApi`.
 */

import {
  browserOidcPrimitives,
  configureApi,
  configureOidc,
} from "@tenir/client-core";

const DEFAULT = "http://localhost:8080";

/**
 * The registered OIDC redirect URI for the web SPA (docs/auth-oidc.md §10). The
 * SPA is served *by* the api, so this is same-origin: Authentik has
 * `<origin>/auth/oidc/callback` registered as the web client's redirect (see
 * `authentik/blueprints/tenir-oidc.yaml` → `TENIR_OIDC_REDIRECT_WEB`). On the way
 * back the browser lands here and `App` completes the exchange from the query
 * string. Based on the api origin (`getServerUrl`) so it matches the registered
 * URI exactly, which Authentik enforces with `matching_mode: strict`.
 */
export function oidcRedirectUri(): string {
  return `${getServerUrl()}/auth/oidc/callback`;
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

// Point the shared REST client at the configured api at startup.
configureApi({ httpBaseUrl: getServerUrl() });
// Wire the browser OIDC primitives (Web Crypto + `location`) so the optional
// "Sign in with Authentik" path can run and so an OIDC session can silently
// refresh on a 401 (docs/auth-oidc.md §10). This only *registers* the seam — it
// is inert until the server advertises OIDC and `prepareOidc` resolves a
// provider, so a deployment with OIDC off behaves exactly as before.
configureOidc(browserOidcPrimitives(oidcRedirectUri()));
