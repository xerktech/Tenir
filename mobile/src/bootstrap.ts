/**
 * App startup wiring (master plan §8.5): point `client-core` at the user's api and
 * give it a secure, device-backed token store before any screen renders.
 *
 * Order matters — we hydrate the persisted token into the synchronous mirror *before*
 * the first `me()` call so a returning user lands straight on their dashboard instead
 * of the login form. Imports the native key/value store, so this runs only on device,
 * never under unit tests.
 */

import { configureTokenStore } from "@tenir/client-core";

import { configureApiFromWs, DEFAULT_WS_URL } from "./config";
import { deviceKeyValue } from "./secureStorage";
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
  await tokens.hydrate();
  configureTokenStore(tokens.store);

  const wsUrl = (await loadServerUrl(kv)) ?? DEFAULT_WS_URL;
  configureApiFromWs(wsUrl);

  const lastTab = await loadLastTab(kv);

  return { wsUrl, lastTab };
}
