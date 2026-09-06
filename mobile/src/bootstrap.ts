/**
 * App startup wiring (master plan §8.5): point `client-core` at the user's api and
 * give it a secure, device-backed token store before any screen renders.
 *
 * Order matters — we hydrate the persisted token into the synchronous mirror *before*
 * the first `me()` call so a returning user lands straight on their dashboard instead
 * of the login form. Imports the native key/value store, so this runs only on device,
 * never under unit tests.
 *
 * OIDC (XERK-655): alongside the token store we hydrate the OIDC sidecar (refresh token
 * + expiry) and inject the native OIDC primitives, so a returning Authentik session can
 * silently refresh instead of bouncing to login when its short-lived access token
 * expires. All of this is inert for a built-in username/password session.
 */

import {
  configureOidc,
  configureOidcStore,
  configureTokenStore,
  getAuthConfig,
  getSessionKind,
  prepareOidc,
} from "@tenir/client-core";

import { configureApiFromWs, DEFAULT_WS_URL } from "./config";
import { nativeOidcPrimitives } from "./native/oidc";
import { deviceKeyValue } from "./secureStorage";
import { createMirroredOidcStore } from "./lib/oidcStore";
import { createMirroredTokenStore, loadLastTab, loadServerUrl } from "./storage";

export interface Bootstrapped {
  /** The api WS URL in effect (persisted choice, or the default seed). */
  wsUrl: string;
  /**
   * The dashboard tab the user was last on, or null on first launch. Restored
   * so relaunching the app keeps the user's place — the mobile equivalent of
   * the web SPA surviving a page refresh (XERK-80).
   */
  lastTab: string | null;
}

/** Configure storage + api and hydrate the saved token. Call once on app launch. */
export async function bootstrap(): Promise<Bootstrapped> {
  const kv = deviceKeyValue();

  const tokens = createMirroredTokenStore(kv);
  const oidc = createMirroredOidcStore(kv);
  await Promise.all([tokens.hydrate(), oidc.hydrate()]);
  configureTokenStore(tokens.store);
  configureOidcStore(oidc.store);
  // Inject the native OIDC primitives + register the silent-refresh seam the transports
  // call on a 401/1008. Inert until a provider is prepared and an OIDC session exists.
  configureOidc(nativeOidcPrimitives());

  const wsUrl = (await loadServerUrl(kv)) ?? DEFAULT_WS_URL;
  configureApiFromWs(wsUrl);

  // A returning OIDC session needs its provider resolved before the token can be
  // silently refreshed. Best-effort: if the server is unreachable or has since turned
  // OIDC off, refresh simply fails later and the user re-logs in — same as built-in.
  if (getSessionKind() === "oidc") {
    try {
      const cfg = await getAuthConfig();
      if (cfg.oidc?.enabled) await prepareOidc(cfg.oidc);
    } catch {
      /* server down / OIDC removed — silent refresh will fall through to re-login */
    }
  }

  const lastTab = await loadLastTab(kv);

  return { wsUrl, lastTab };
}
