---
paths:
  - "mobile/src/lib/oidc*.ts"
  - "mobile/src/native/oidc.ts"
  - "mobile/src/bootstrap.ts"
  - "mobile/src/screens/Setup.tsx"
  - "mobile/android/app/src/main/AndroidManifest.xml"
---

# Mobile native OIDC (XERK-655)

- The **entire** OIDC state machine (PKCE, `state`/`nonce`, code exchange, silent refresh,
  RP-initiated logout) lives in `@tenir/client-core` (`oidc.ts`/`auth.ts`, T7). Mobile only
  injects platform primitives via `configureOidc`/`configureOidcStore`. Do not reimplement any
  of that flow on the mobile side.
- **Do not use `react-native-app-auth` (or expo-auth-session's `authorize`).** Their
  `authorize()` runs their own discovery/PKCE/token-exchange and return minted tokens — that
  *bypasses* client-core's flow (nonce binding, shape-agnostic token store, shared refresh/logout
  seams). The contract client-core exposes is "open this URL, hand back the callback params"; that
  is all `native/oidc.ts` provides, via React Native's first-party `Linking` + `AppState`.
- **The redirect URI is a 3-place contract — change all together or the IdP rejects the login:**
  `MOBILE_REDIRECT_URI` (`mobile/src/native/oidc.ts`), the custom-scheme `<intent-filter>` on
  `MainActivity` (`AndroidManifest.xml`), and `TENIR_OIDC_REDIRECT_MOBILE`
  (`authentik/blueprints/tenir-oidc.yaml`, default `com.xerktech.tenir://auth/callback`).
- **Hydrate the OIDC sidecar at startup or silent refresh breaks on relaunch.** The refresh token +
  expiry live in the keychain-mirrored `OidcStore` (`lib/oidcStore.ts`); `bootstrap` must hydrate it
  *and* re-resolve the provider (`prepareOidc`) for a returning OIDC session, else `getSessionKind`
  reads "builtin", no refresh is attempted, and the app bounces to login when the token expires.
- Secure randomness comes from `crypto.getRandomValues` (polyfilled by
  `react-native-get-random-values`, imported in `index.js` before any OIDC code). `native/oidc.ts`
  **throws** if it is missing rather than falling back to weak randomness for the PKCE verifier.
- Keep the built-in username/password path untouched: the Authentik button shows **only** when the
  entered server's `/auth/config` advertises `oidc.enabled`. Per-user History needs no mobile change
  — the server scopes recordings by the token's user regardless of login method.
