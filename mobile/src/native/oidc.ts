/**
 * Native OIDC primitives for the Android app (XERK-655) — the thin, device-only wiring
 * that binds `client-core`'s injected-primitive OIDC flow (T7) to React Native's OS
 * APIs. The testable logic lives in `lib/oidcPrimitives.ts`; this module only supplies
 * the real seams and is therefore loaded on device (via `bootstrap.ts`), never under
 * the vitest suite.
 *
 * - **Secure random:** `crypto.getRandomValues`, polyfilled by `react-native-get-random-values`
 *   (imported once in `index.js` before any OIDC code runs).
 * - **Redirect:** `Linking.openURL` opens the IdP authorize URL in the system browser;
 *   the custom-scheme redirect (`MOBILE_REDIRECT_URI`, registered as an Android
 *   intent-filter on `MainActivity`) deep-links back and arrives on `Linking`'s `url`
 *   event. `AppState` reports the return-to-foreground used to detect a user cancel.
 */

import { AppState, Linking, type AppStateStatus } from "react-native";

import type { OidcPrimitives } from "@tenir/client-core";
import { buildNativeOidcPrimitives, type RandomBytes } from "../lib/oidcPrimitives";

/**
 * The custom-scheme redirect the app registers with Authentik (contract:
 * `TENIR_OIDC_REDIRECT_MOBILE`, `authentik/blueprints/tenir-oidc.yaml`). Must match the
 * `<intent-filter>` on `MainActivity` in the Android manifest exactly.
 */
export const MOBILE_REDIRECT_URI = "com.xerktech.tenir://auth/callback";

const randomBytes: RandomBytes = (length) => {
  const out = new Uint8Array(length);
  const c = (globalThis as { crypto?: Crypto }).crypto;
  if (!c?.getRandomValues) {
    // react-native-get-random-values must be imported at startup (index.js). Without a
    // CSPRNG we must not fall back to a weak PKCE verifier — fail the login instead.
    throw new Error("secure random unavailable — react-native-get-random-values not loaded");
  }
  c.getRandomValues(out);
  return out;
};

/** Build the AppAuth-style native primitives `configureOidc` is called with at startup. */
export function nativeOidcPrimitives(): OidcPrimitives {
  return buildNativeOidcPrimitives({
    randomBytes,
    redirectUri: MOBILE_REDIRECT_URI,
    openUrl: (url) => Linking.openURL(url),
    onUrl: (handler) => {
      const sub = Linking.addEventListener("url", (event: { url: string }) => handler(event.url));
      return () => sub.remove();
    },
    onForeground: (handler) => {
      let prev: AppStateStatus = AppState.currentState;
      const sub = AppState.addEventListener("change", (next: AppStateStatus) => {
        if ((prev === "background" || prev === "inactive") && next === "active") handler();
        prev = next;
      });
      return () => sub.remove();
    },
  });
}
