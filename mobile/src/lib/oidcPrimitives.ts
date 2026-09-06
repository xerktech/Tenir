/**
 * The mobile half of the optional Authentik login (XERK-655): the platform-injected
 * OIDC primitives that `client-core`'s flow (`oidc.ts`, T7/XERK-653) drives.
 *
 * T7 owns the entire OIDC state machine — PKCE, `state`/`nonce`, the code exchange,
 * silent refresh, and RP-initiated logout — and asks the host only for a handful of
 * environment primitives: PKCE crypto, secure randomness, the app's redirect URI, and
 * a `redirect` that opens the IdP and hands back the callback. On the browser fronts
 * (`web`/`even`) that redirect navigates the page; on native it opens a system browser
 * and resolves with the custom-scheme callback params, so `startOidcLogin` completes
 * the exchange inline (see `OidcPrimitives.redirect` in `client-core/src/oidc.ts`).
 *
 * This module is deliberately React-Native-agnostic: the OS-specific seams — secure
 * random bytes, opening a URL, the inbound-URL and foreground event streams — are all
 * injected. `native/oidc.ts` wires the real RN implementations (`crypto.getRandomValues`
 * from `react-native-get-random-values`, and `Linking`/`AppState`); the tests inject
 * deterministic fakes, so PKCE and the redirect handshake are exercised under vitest
 * with no device, browser, or IdP.
 *
 * Why not `react-native-app-auth`? Its `authorize()` runs its *own* discovery, PKCE,
 * and token exchange and returns already-minted tokens — it cannot hand back a raw
 * authorization code, so it would bypass the whole T7 flow (nonce binding, the
 * shape-agnostic token store, the shared refresh/logout seams) rather than feed it.
 * The contract T7 exposes is "open this URL, give me back the callback"; React
 * Native's first-party `Linking` provides exactly that, opening the system browser
 * (per the acceptance criteria) with no extra native module. SHA-256 for the PKCE
 * challenge is the one primitive RN lacks (no `crypto.subtle`), so it comes from the
 * pure-JS `js-sha256` — hashing the (single-use, client-generated) verifier is a
 * public transform, not a secret-key operation.
 */

import { sha256 } from "js-sha256";

import { OidcError, type OidcCallback, type OidcPrimitives, type Pkce } from "@tenir/client-core";

const BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/**
 * base64url-encode raw bytes with no padding (RFC 4648 §5). Pure and portable so it
 * matches on device (React Native, where `btoa` may be absent) and under node/vitest
 * without depending on either `btoa` or `Buffer`.
 */
export function base64UrlEncode(bytes: Uint8Array): string {
  let out = "";
  let i = 0;
  for (; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
    out += BASE64URL[(n >> 18) & 63] + BASE64URL[(n >> 12) & 63] + BASE64URL[(n >> 6) & 63] + BASE64URL[n & 63];
  }
  const rem = bytes.length - i;
  if (rem === 1) {
    const n = bytes[i] << 16;
    out += BASE64URL[(n >> 18) & 63] + BASE64URL[(n >> 12) & 63];
  } else if (rem === 2) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8);
    out += BASE64URL[(n >> 18) & 63] + BASE64URL[(n >> 12) & 63] + BASE64URL[(n >> 6) & 63];
  }
  return out;
}

/** Source of secure random bytes (the OS CSPRNG on device; a deterministic fake in tests). */
export type RandomBytes = (length: number) => Uint8Array;

/**
 * Generate a PKCE pair: a 32-byte base64url `verifier` and its S256 `challenge`
 * (base64url(SHA-256(verifier))). Matches `browserOidcPrimitives` byte-for-byte so the
 * two platforms produce the same shape (cross-platform-parity rule).
 */
export function createPkce(randomBytes: RandomBytes): Pkce {
  const verifier = base64UrlEncode(randomBytes(32));
  const challenge = base64UrlEncode(Uint8Array.from(sha256.array(verifier)));
  return { verifier, challenge };
}

/** A high-entropy URL-safe random string (16 bytes), for `state` and `nonce`. */
export function randomString(randomBytes: RandomBytes): string {
  return base64UrlEncode(randomBytes(16));
}

/**
 * Parse an inbound deep-link URL into OIDC callback params, or `null` when the URL is
 * not our redirect (some other deep link arrived on the same listener). The IdP appends
 * `?code&state` (or `?error&error_description`) to the registered redirect URI.
 */
export function parseOidcRedirect(url: string, redirectUri: string): OidcCallback | null {
  if (!url.startsWith(redirectUri)) return null;
  const q = url.indexOf("?");
  const params = new URLSearchParams(q >= 0 ? url.slice(q + 1) : "");
  return {
    code: params.get("code") ?? undefined,
    state: params.get("state") ?? undefined,
    error: params.get("error") ?? undefined,
    error_description: params.get("error_description") ?? undefined,
  };
}

/** OS seams the native redirect needs, injected so the handshake is unit-testable. */
export interface RedirectDeps {
  /** The app's registered custom-scheme redirect URI. */
  redirectUri: string;
  /** Open a URL in the system browser (Android hands the authorize URL to a browser/tab). */
  openUrl(url: string): Promise<unknown>;
  /** Subscribe to inbound deep-link URLs; returns an unsubscribe function. */
  onUrl(handler: (url: string) => void): () => void;
  /** Subscribe to the app returning to the foreground; returns an unsubscribe function. */
  onForeground(handler: () => void): () => void;
  /**
   * How long to wait, after the app returns to the foreground without a redirect, before
   * treating the login as cancelled. A real redirect deep-links in well under this; the
   * delay only lets a near-simultaneous URL event win the race against the foreground one.
   */
  cancelGraceMs?: number;
}

/**
 * Build the native `redirect(url)` primitive: open the IdP authorize URL in the system
 * browser and resolve with the callback params once the custom-scheme redirect returns
 * to the app. Rejects with an `OidcError` if the user backs out of the browser without
 * completing (so the UI doesn't hang on "Signing in…"), or if the URL fails to open.
 */
export function makeOidcRedirect(deps: RedirectDeps): (authorizeUrl: string) => Promise<OidcCallback> {
  const graceMs = deps.cancelGraceMs ?? 400;
  return (authorizeUrl) =>
    new Promise<OidcCallback>((resolve, reject) => {
      let settled = false;
      let sawRedirect = false;
      let unUrl = () => {};
      let unFg = () => {};
      const finish = (run: () => void) => {
        if (settled) return;
        settled = true;
        unUrl();
        unFg();
        run();
      };

      unUrl = deps.onUrl((url) => {
        const cb = parseOidcRedirect(url, deps.redirectUri);
        if (!cb) return; // an unrelated deep link — keep waiting for ours
        sawRedirect = true;
        finish(() => resolve(cb));
      });

      unFg = deps.onForeground(() => {
        // Back in the app. If the redirect already arrived we're done; otherwise the
        // user dismissed the browser — give a real redirect a moment to land, then
        // treat the return as a cancel.
        setTimeout(() => {
          if (sawRedirect) return;
          finish(() => reject(new OidcError("Sign-in was cancelled")));
        }, graceMs);
      });

      deps.openUrl(authorizeUrl).catch((err: unknown) =>
        finish(() => reject(new OidcError(`Could not open the sign-in page: ${String(err)}`))),
      );
    });
}

/** Assemble the full set of native OIDC primitives from the OS seams. */
export function buildNativeOidcPrimitives(
  deps: RedirectDeps & { randomBytes: RandomBytes },
): OidcPrimitives {
  const redirect = makeOidcRedirect(deps);
  return {
    createPkce: () => createPkce(deps.randomBytes),
    randomString: () => randomString(deps.randomBytes),
    redirectUri: deps.redirectUri,
    redirect,
  };
}
