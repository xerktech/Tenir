/**
 * Web SPA api configuration.
 *
 * The SPA is built into the api container image and served by the api itself, so
 * in production the api is simply the page's own origin. `VITE_API_HTTP` still
 * seeds a different api URL for local dev (Vite dev server on :5174 talking to an
 * api elsewhere). The resolved URL is pushed into the shared REST client via
 * `configureApi`.
 */

import { configureApi } from "@tenir/client-core";

const DEFAULT = "http://localhost:8080";

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
