/**
 * Optional OIDC Authorization Code + PKCE flow (docs/auth-oidc.md §10).
 *
 * The client-side half of the household's optional Authentik login. It sits *beside*
 * the built-in username/password `login()` in `api.ts`: a deployment with OIDC off
 * never reaches this module and behaves byte-for-byte as before. When the server
 * advertises OIDC (`getAuthConfig` → `oidc.enabled`), a UI can additionally offer an
 * OIDC button that drives this flow.
 *
 * Everything environment-specific is injected via `configureOidc`, matching the
 * package's DI style (`configureApi`, `configureTokenStore`): PKCE crypto and secure
 * randomness, the redirect mechanism, and the app's redirect URI. Two shapes of
 * `redirect` cover both targets:
 *
 *  - **Browser (web / even):** `redirect(url)` navigates the page and never returns
 *    (the page unloads). On the way back the callback screen calls
 *    `completeOidcCallback(window.location.search)`.
 *  - **Native (mobile / AppAuth):** `redirect(url)` opens the system browser and
 *    resolves with `{ code, state }` once the user returns, so `startOidcLogin`
 *    completes the exchange inline and resolves with the `Principal`.
 *
 * The access token from the exchange is stored via the shape-agnostic `TokenStore`
 * (so REST/WS auth is unchanged); the refresh token + expiry go in the `OidcStore`
 * sidecar. Silent refresh happens before expiry, and on any 401/1008 the transports
 * call back through `tryOidcRefresh` (registered here) to refresh-then-retry.
 */

import { me, NetworkError, type Principal, type ServerAuthConfig } from "./api";
import {
  clearOidcSession,
  clearOidcTransaction,
  clearToken,
  getOidcSession,
  getOidcTransaction,
  registerOidcRefresher,
  setOidcSession,
  setOidcTransaction,
  setToken,
} from "./auth";

/** A generated PKCE pair: the secret `verifier` and its S256 `challenge`. */
export interface Pkce {
  verifier: string;
  /** base64url(SHA-256(verifier)) — sent as `code_challenge` with method S256. */
  challenge: string;
}

/**
 * Environment primitives the OIDC flow needs, injected by the host frontend.
 * Kept as narrow function types so a browser wires Web Crypto + `location`, native
 * wires AppAuth, and tests wire deterministic fakes — no real browser/IdP needed.
 */
export interface OidcPrimitives {
  /** Generate a PKCE verifier and its S256 challenge. */
  createPkce(): Promise<Pkce> | Pkce;
  /** A high-entropy URL-safe random string, for `state` and `nonce`. */
  randomString(): string;
  /**
   * The app's registered redirect URI (e.g. `https://app/oidc/callback`, or a
   * native `app://oidc`), echoed in the authorize request and the token exchange.
   */
  redirectUri: string;
  /**
   * Send the user to the IdP authorize URL. Browser: navigate, return void/undefined
   * (the page unloads). Native: open a browser tab and resolve with the callback's
   * `code` and `state` once the user returns.
   */
  redirect(url: string): Promise<OidcCallback | void> | OidcCallback | void;
}

/** The parameters the IdP hands back to the redirect URI after authorization. */
export interface OidcCallback {
  code?: string;
  state?: string;
  error?: string;
  error_description?: string;
}

/** The resolved IdP endpoints the flow drives, merged from `/auth/config` + discovery. */
export interface OidcProvider {
  issuer: string;
  clientId: string;
  scopes: string[];
  authorizationEndpoint: string;
  tokenEndpoint: string;
  /** RP-initiated logout endpoint; absent if the IdP doesn't advertise one. */
  endSessionEndpoint?: string;
}

/** A failure in the OIDC flow that isn't a transport error (`NetworkError`). */
export class OidcError extends Error {
  constructor(
    message: string,
    /** The IdP `error` code when the failure came back on the callback/token response. */
    readonly code?: string,
  ) {
    super(message);
    this.name = "OidcError";
  }
}

// Refresh a little before the token actually expires, so an in-flight request never
// races expiry; also the window `refreshBeforeExpiry` treats a token as "due".
const REFRESH_SKEW_MS = 60_000;

let primitives: OidcPrimitives | null = null;
let provider: OidcProvider | null = null;

/**
 * Inject the environment primitives and register the silent-refresh seam. Call once
 * at startup on a frontend that offers OIDC. Registering the refresher here (rather
 * than importing this module from the transports) is what lets `api.ts`/`ws.ts`
 * refresh-then-retry on a 401/1008 without a circular import.
 */
export function configureOidc(p: OidcPrimitives): void {
  primitives = p;
  registerOidcRefresher(() => refreshIfPossible());
}

/** Set the resolved provider directly (tests, or a host that discovered it itself). */
export function setOidcProvider(p: OidcProvider): void {
  provider = p;
}

/** The resolved provider, or null until `prepareOidc`/`setOidcProvider` has run. */
export function getOidcProvider(): OidcProvider | null {
  return provider;
}

/** Whether the OIDC flow is ready to start (primitives injected and a provider known). */
export function oidcReady(): boolean {
  return primitives !== null && provider !== null;
}

// ---- discovery / preparation ------------------------------------------------

/** The subset of the OpenID discovery document the flow uses. */
interface Discovery {
  authorization_endpoint?: string;
  token_endpoint?: string;
  end_session_endpoint?: string;
}

/**
 * Fetch `{issuer}/.well-known/openid-configuration`. The `/auth/config` advertisement
 * (§10) guarantees only `authorizationEndpoint`; the token and end-session endpoints
 * come from discovery.
 */
export async function discoverOidc(issuer: string): Promise<Discovery> {
  const base = issuer.endsWith("/") ? issuer : `${issuer}/`;
  const url = `${base}.well-known/openid-configuration`;
  let res: Response;
  try {
    res = await fetch(url);
  } catch (cause) {
    throw new NetworkError("could not reach the OIDC issuer", cause);
  }
  if (!res.ok) throw new OidcError(`OIDC discovery failed (${res.status})`);
  return (await res.json()) as Discovery;
}

/**
 * Resolve and store the provider from the server's advertisement + IdP discovery.
 * Returns the merged `OidcProvider`, or throws if the config is unusable. Callers
 * that only need "should I show the button?" should look at
 * `getAuthConfig().oidc?.enabled` instead — this fetches discovery and is meant for
 * a frontend about to actually offer login.
 */
export async function prepareOidc(
  serverOidc: NonNullable<ServerAuthConfig["oidc"]>,
): Promise<OidcProvider> {
  const disc = await discoverOidc(serverOidc.issuer);
  const authorizationEndpoint = serverOidc.authorizationEndpoint ?? disc.authorization_endpoint;
  const tokenEndpoint = disc.token_endpoint;
  if (!authorizationEndpoint || !tokenEndpoint) {
    throw new OidcError("OIDC discovery is missing authorization/token endpoints");
  }
  provider = {
    issuer: serverOidc.issuer,
    clientId: serverOidc.clientId,
    scopes: serverOidc.scopes?.length ? serverOidc.scopes : ["openid", "email", "profile", "groups"],
    authorizationEndpoint,
    tokenEndpoint,
    endSessionEndpoint: disc.end_session_endpoint,
  };
  return provider;
}

// ---- authorize + callback ---------------------------------------------------

function requireReady(): { p: OidcPrimitives; prov: OidcProvider } {
  if (!primitives) throw new OidcError("OIDC not configured — call configureOidc first");
  if (!provider) throw new OidcError("OIDC provider unknown — call prepareOidc first");
  return { p: primitives, prov: provider };
}

/** Build the IdP authorize URL for a PKCE transaction. Exported for unit testing. */
export function buildAuthorizeUrl(
  prov: OidcProvider,
  redirectUri: string,
  tx: { challenge: string; state: string; nonce: string },
): string {
  const u = new URL(prov.authorizationEndpoint);
  u.searchParams.set("response_type", "code");
  u.searchParams.set("client_id", prov.clientId);
  u.searchParams.set("redirect_uri", redirectUri);
  u.searchParams.set("scope", prov.scopes.join(" "));
  u.searchParams.set("state", tx.state);
  u.searchParams.set("nonce", tx.nonce);
  u.searchParams.set("code_challenge", tx.challenge);
  u.searchParams.set("code_challenge_method", "S256");
  return u.toString();
}

/**
 * Begin an OIDC login. Generates PKCE + `state` + `nonce`, persists them (so the
 * browser redirect round-trip survives a full page navigation), builds the authorize
 * URL, and hands it to the injected `redirect`.
 *
 * Resolves with the `Principal` when `redirect` is the native shape (returns the
 * callback inline). Resolves with `undefined` when `redirect` navigates the browser
 * away — the page unloads before this settles, and the callback screen finishes the
 * flow via `completeOidcCallback`.
 */
export async function startOidcLogin(): Promise<Principal | undefined> {
  const { p, prov } = requireReady();
  const pkce = await p.createPkce();
  const state = p.randomString();
  const nonce = p.randomString();
  setOidcTransaction({ verifier: pkce.verifier, state, nonce });
  const url = buildAuthorizeUrl(prov, p.redirectUri, { challenge: pkce.challenge, state, nonce });
  const result = await p.redirect(url);
  if (result) return completeOidcCallback(result);
  return undefined;
}

/** Parse a callback into its params, accepting a query string, `URLSearchParams`, or object. */
function parseCallback(input: OidcCallback | URLSearchParams | string): OidcCallback {
  if (typeof input === "string") {
    const q = new URLSearchParams(input.startsWith("?") ? input.slice(1) : input);
    input = q;
  }
  if (input instanceof URLSearchParams) {
    return {
      code: input.get("code") ?? undefined,
      state: input.get("state") ?? undefined,
      error: input.get("error") ?? undefined,
      error_description: input.get("error_description") ?? undefined,
    };
  }
  return input;
}

/**
 * Complete the redirect callback: validate `state` against the saved transaction,
 * exchange `code` + `code_verifier` for tokens, store them, and confirm identity via
 * `me()`. Accepts the callback as an object, a `URLSearchParams`, or a raw query
 * string (`window.location.search`).
 */
export async function completeOidcCallback(
  input: OidcCallback | URLSearchParams | string,
): Promise<Principal> {
  const { prov } = requireReady();
  const cb = parseCallback(input);
  const tx = getOidcTransaction();
  // Always burn the transaction: it is single-use, and leaving it lets a stale
  // verifier/state be replayed against a later callback.
  clearOidcTransaction();

  if (cb.error) {
    throw new OidcError(cb.error_description || `OIDC authorization failed: ${cb.error}`, cb.error);
  }
  if (!tx) throw new OidcError("no pending OIDC login (missing PKCE transaction)");
  if (!cb.state || cb.state !== tx.state) {
    throw new OidcError("OIDC state mismatch — possible CSRF, login rejected");
  }
  if (!cb.code) throw new OidcError("OIDC callback missing authorization code");

  const tokens = await exchangeToken(prov, {
    grant_type: "authorization_code",
    code: cb.code,
    redirect_uri: requireReady().p.redirectUri,
    code_verifier: tx.verifier,
  });
  if (tokens.id_token) assertNonce(tokens.id_token, tx.nonce);
  storeSession(tokens);
  try {
    return await me();
  } catch (err) {
    // The token didn't confirm — don't leave a half-session behind.
    clearToken();
    clearOidcSession();
    throw err;
  }
}

// ---- token exchange + refresh -----------------------------------------------

interface TokenResponse {
  access_token: string;
  refresh_token?: string;
  id_token?: string;
  expires_in?: number;
  token_type?: string;
}

async function exchangeToken(
  prov: OidcProvider,
  params: Record<string, string>,
): Promise<TokenResponse> {
  const body = new URLSearchParams({ ...params, client_id: prov.clientId });
  let res: Response;
  try {
    res = await fetch(prov.tokenEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
  } catch (cause) {
    throw new NetworkError("could not reach the OIDC token endpoint", cause);
  }
  if (!res.ok) {
    let detail = res.statusText;
    let code: string | undefined;
    try {
      const data = (await res.json()) as { error?: string; error_description?: string };
      code = data.error;
      detail = data.error_description || data.error || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new OidcError(`OIDC token request failed (${res.status}): ${detail}`, code);
  }
  return (await res.json()) as TokenResponse;
}

/** Persist an access token + its refresh sidecar from a token response. */
function storeSession(tokens: TokenResponse): void {
  setToken(tokens.access_token);
  // Authentik may rotate the refresh token; keep the prior one if none came back.
  const previous = getOidcSession();
  setOidcSession({
    refreshToken: tokens.refresh_token ?? previous?.refreshToken ?? null,
    expiresAt: tokens.expires_in ? Date.now() + tokens.expires_in * 1000 : null,
  });
}

/**
 * Silently exchange the stored refresh token for a fresh access token. Throws if
 * there is no OIDC session or no refresh token; the caller decides whether that
 * surfaces as re-login.
 */
export async function refreshOidcSession(): Promise<void> {
  const { prov } = requireReady();
  const session = getOidcSession();
  if (!session?.refreshToken) throw new OidcError("no OIDC refresh token available");
  const tokens = await exchangeToken(prov, {
    grant_type: "refresh_token",
    refresh_token: session.refreshToken,
  });
  storeSession(tokens);
}

/** Whether the current OIDC access token is expired or within the refresh skew window. */
export function oidcTokenDue(now = Date.now()): boolean {
  const session = getOidcSession();
  if (!session) return false;
  if (session.expiresAt === null) return false;
  return now >= session.expiresAt - REFRESH_SKEW_MS;
}

/**
 * Refresh the access token if it is due (before expiry). No-op for a built-in session
 * or a still-fresh OIDC token. A frontend calls this proactively (e.g. before opening
 * the WS or on app foreground) to keep the session alive without a 401 round-trip.
 */
export async function ensureFreshOidcToken(): Promise<void> {
  if (getSessionIsOidc() && oidcTokenDue()) await refreshOidcSession();
}

function getSessionIsOidc(): boolean {
  return getOidcSession() !== null;
}

/**
 * The refresher registered with the transports. Refreshes only an OIDC session with
 * a refresh token, and reports success/failure as a boolean (never throws) so a 401
 * handler can cleanly fall through to re-login.
 */
async function refreshIfPossible(): Promise<boolean> {
  if (!provider || !getSessionIsOidc()) return false;
  try {
    await refreshOidcSession();
    return true;
  } catch {
    return false;
  }
}

// ---- logout -----------------------------------------------------------------

/**
 * RP-initiated logout (§10): clear the local token + OIDC sidecar, then end the IdP
 * session by sending the user to the provider's `end_session_endpoint` (if one is
 * advertised) via the injected `redirect`. Falls back to a local-only logout when no
 * end-session endpoint or redirect is available.
 */
export async function oidcLogout(): Promise<void> {
  const prov = provider;
  clearToken();
  clearOidcSession();
  clearOidcTransaction();
  if (!prov?.endSessionEndpoint || !primitives) return;
  const u = new URL(prov.endSessionEndpoint);
  u.searchParams.set("client_id", prov.clientId);
  u.searchParams.set("post_logout_redirect_uri", primitives.redirectUri);
  await primitives.redirect(u.toString());
}

// ---- browser primitives -----------------------------------------------------

/** base64url-encode raw bytes (no padding), for PKCE verifier/challenge and randoms. */
function base64UrlEncode(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  const g = globalThis as { btoa?: (s: string) => string };
  const b64 = g.btoa ? g.btoa(s) : "";
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * OIDC primitives for a browser front (web / even), built on Web Crypto + `location`.
 * Both browser fronts share this — the cross-platform-parity rule means the OIDC
 * button behaves identically on each without either reimplementing PKCE. The mobile
 * app injects its own AppAuth-backed primitives instead.
 *
 * `redirect` navigates the page (returns void), so `startOidcLogin` does not resolve
 * inline; the callback route calls `completeOidcCallback(window.location.search)`.
 */
export function browserOidcPrimitives(redirectUri: string): OidcPrimitives {
  const cryptoObj = globalThis.crypto;
  return {
    async createPkce(): Promise<Pkce> {
      const raw = new Uint8Array(32);
      cryptoObj.getRandomValues(raw);
      const verifier = base64UrlEncode(raw);
      const digest = await cryptoObj.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
      return { verifier, challenge: base64UrlEncode(new Uint8Array(digest)) };
    },
    randomString(): string {
      const raw = new Uint8Array(16);
      cryptoObj.getRandomValues(raw);
      return base64UrlEncode(raw);
    },
    redirectUri,
    redirect(url: string): void {
      window.location.assign(url);
    },
  };
}

// ---- id_token nonce check ---------------------------------------------------

/**
 * Verify the `id_token`'s `nonce` matches the one we sent, binding the token to this
 * login and blocking replay of a token minted for a different session. A lightweight
 * base64url payload decode — the API is what cryptographically validates the token
 * (RS256/JWKS, §4); the client only checks the nonce it chose.
 */
function assertNonce(idToken: string, expected: string): void {
  const parts = idToken.split(".");
  if (parts.length !== 3) throw new OidcError("id_token is not a JWT");
  let claims: { nonce?: string };
  try {
    claims = JSON.parse(base64UrlDecode(parts[1])) as { nonce?: string };
  } catch {
    throw new OidcError("id_token payload is not valid JSON");
  }
  if (claims.nonce !== expected) throw new OidcError("id_token nonce mismatch — login rejected");
}

const B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/**
 * Decode a base64url string to its raw bytes-as-latin1 string. Uses the platform
 * `atob` when present (browser, jsdom, RN) and a small pure-JS fallback otherwise, so
 * the flow needs no Node `Buffer` and no polyfill. We only decode the id_token
 * payload for the nonce check — it is not a security boundary (the API validates the
 * token cryptographically), so a compact decoder is enough.
 */
function base64UrlDecode(input: string): string {
  const b64 = input.replace(/-/g, "+").replace(/_/g, "/");
  const g = globalThis as { atob?: (s: string) => string };
  if (typeof g.atob === "function") return g.atob(b64);
  let out = "";
  let bits = 0;
  let value = 0;
  for (const ch of b64) {
    const idx = B64_ALPHABET.indexOf(ch);
    if (idx === -1) continue; // skip padding / whitespace
    value = (value << 6) | idx;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out += String.fromCharCode((value >> bits) & 0xff);
    }
  }
  return out;
}
