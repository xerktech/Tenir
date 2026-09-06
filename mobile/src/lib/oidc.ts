/**
 * Mobile OIDC orchestration (XERK-655): the small glue between the setup screen / auth
 * controller and `client-core`'s OIDC flow. Imports only `react` and `client-core` (never
 * `react-native`), so it is unit-tested under vitest exactly like the other controllers.
 *
 * The heavy lifting — PKCE, the code exchange, silent refresh, RP-initiated logout — lives
 * in `client-core` (T7); this only decides *whether* a given server offers OIDC and kicks
 * off the flow the setup screen's button drives.
 */

import { getAuthConfig, prepareOidc, startOidcLogin } from "@tenir/client-core";

/**
 * Ask the (already-configured) server whether it advertises OIDC, and if so resolve and
 * cache its provider so a subsequent `startOidcLogin` can run. Returns whether the "Sign
 * in with Authentik" button should be shown. Assumes `configureApiFromWs` has already
 * pointed `client-core` at the server in question.
 */
export async function probeOidcAvailable(): Promise<boolean> {
  const cfg = await getAuthConfig();
  if (!cfg.oidc?.enabled) return false;
  // Resolve the provider now (fetches IdP discovery) so both the login button and the
  // transports' silent-refresh seam have a provider to work with.
  await prepareOidc(cfg.oidc);
  return true;
}

/**
 * Begin the native OIDC login: open Authentik in the system browser and, on return via
 * the custom scheme, complete the PKCE code exchange in-app. Resolves once the session is
 * established (token stored, identity confirmed). The provider must already be resolved
 * (`probeOidcAvailable`).
 */
export async function signInWithOidc(): Promise<void> {
  await startOidcLogin();
}
