/**
 * Api REST client.
 *
 * Thin typed wrapper over the api's REST surface — auth, users, history/search,
 * and the component status. Every call carries the bearer token via
 * `authHeader()`; a non-2xx response throws an `ApiError` carrying the status and
 * the server's detail message. Shared by every TS frontend.
 */

import {
  authHeader,
  clearOidcSession,
  clearToken,
  getToken,
  setToken,
  tryOidcRefresh,
} from "./auth";
import { apiBaseUrl } from "./config";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * A transport-level failure: the request never reached the api (server down, DNS
 * failure, offline, blocked by CORS). Distinct from `ApiError`, which means the
 * server answered with a non-2xx status. Carries the underlying cause for logs.
 */
export class NetworkError extends Error {
  constructor(
    message: string,
    readonly cause?: unknown,
  ) {
    super(message);
    this.name = "NetworkError";
  }
}

interface RequestOptions {
  /**
   * Send the bearer token. Default true; `/auth/login` opts out (see `login`).
   */
  auth?: boolean;
  /**
   * Internal: set once we've already refreshed-and-retried after a 401, so an
   * OIDC session that still 401s surfaces the error instead of looping forever.
   */
  retried?: boolean;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = opts.auth === false ? {} : { ...authHeader() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`${apiBaseUrl()}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (cause) {
    // fetch rejects only on a transport failure, never on an HTTP error status —
    // surface it as a typed NetworkError so callers can tell "can't reach the
    // server" apart from "the server said no".
    throw new NetworkError("could not reach the server", cause);
  }
  // OIDC silent refresh (docs/auth-oidc.md §10): an authenticated 401 on an OIDC
  // session means the access token expired mid-use — refresh it against the IdP and
  // retry the request once. `tryOidcRefresh` is a no-op returning false for a
  // built-in session (its token rides X-Renewed-Token instead) and when no OIDC is
  // configured, so this path is inert for the built-in flow. The retry flag stops a
  // still-401 (revoked/invalid refresh) from looping — it falls through to throw.
  if (res.status === 401 && opts.auth !== false && !opts.retried) {
    if (await tryOidcRefresh()) {
      return request<T>(method, path, body, { ...opts, retried: true });
    }
  }
  // Sliding renewal (XERK-168): past half a token's life the api attaches a fresh
  // one to every authenticated response. Adopting it here — the one request path
  // every frontend shares — is what keeps a device logged in until it explicitly
  // logs out, instead of being bounced to the login screen when the token expires.
  // Only from a request that PRESENTED a token (XERK-237). Adopting it off ANY
  // response let an unauthenticated one — a login against a mistyped or hostile
  // address — hand back a replacement the device stored, leaving the real server
  // 401ing. Gating on `res.ok` instead would also have worked for that, but it
  // quietly broke the renewal above: the api's middleware runs after the route
  // with no status check, so an aged-but-valid token is renewed on authenticated
  // 404s and 422s too, and those renewals must still be taken.
  if (opts.auth !== false) {
    const renewed = res.headers.get("x-renewed-token");
    if (renewed) setToken(renewed);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = (await res.json()) as { detail?: string };
      if (data.detail) detail = data.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- shapes (mirror the api response models) ---------------------------

export interface Principal {
  userId: string;
  username: string;
  household: string;
  role: string;
}

/** A household member in the admin roster. */
export interface User {
  userId: string;
  username: string;
  role: string;
  /** The env-managed bootstrap admin — reconciled from env on boot, can't be removed. */
  isEnvAdmin: boolean;
}

export interface ConversationSummary {
  id: string;
  status: string;
  micSource: string | null;
  sourceLang: string | null;
  startedAt: string;
  endedAt: string | null;
  durationMs: number;
  segmentCount: number;
  hasAudio: boolean;
}

export interface SegmentView {
  segmentId: string;
  text: string;
  startMs: number;
  endMs: number;
  lang: string | null;
  /** English translation of a non-English turn (XERK-160); absent/null otherwise. */
  translation?: string | null;
}

/** A private context cue, rendered inline in history at atMs (XERK-81). */
export interface CueView {
  cueId: string;
  title: string;
  body: string;
  atMs: number;
  /** Live-source attribution (XERK-120); null for a cue from model knowledge. */
  source?: string | null;
}

/** A song recognized playing, rendered inline in history at atMs (XERK-184). */
export interface SongView {
  songId: string;
  title: string;
  artist: string;
  atMs: number;
  durationMs?: number | null;
}

export interface Conversation extends ConversationSummary {
  segments: SegmentView[];
  cues: CueView[];
  /** Songs recognized playing during the session (XERK-184). Absent on payloads
   *  written before the feature. */
  songs?: SongView[];
}

export type ComponentState = "ready" | "connecting" | "down";

export interface ComponentStatus {
  id: string;
  label: string;
  category: "infra" | "model" | "gateway";
  state: ComponentState;
  detail: string;
  checkedAt: string;
}

export interface SystemStatus {
  overall: "ready" | "degraded" | "down";
  generatedAt: string;
  reasons: string[];
  components: ComponentStatus[];
}

// ---- auth -------------------------------------------------------------------

/**
 * Sign in. The credentials are the ONLY thing sent (XERK-237): the bearer token
 * is deliberately withheld, because a login carries no authority worth proving
 * and attaching it handed the user's live token to whatever server the
 * (user-typed, self-hosted) address named — turning a mistyped or phished
 * address into a credential leak rather than just a failed sign-in.
 *
 * Two round-trips, so the new token is only PROVISIONAL until `me()` confirms
 * it: a server that accepts `/auth/login` and then rejects `/auth/me` must not
 * destroy the token the user already had on an attempt that reports failure.
 */
export async function login(username: string, password: string): Promise<Principal> {
  const out = await request<{ token: string }>(
    "POST",
    "/auth/login",
    { username, password },
    { auth: false },
  );
  const previous = getToken();
  setToken(out.token);
  try {
    const principal = await me();
    // A confirmed built-in login supersedes any prior OIDC session, so the session
    // kind reverts to built-in (no more silent-refresh attempts on this token).
    clearOidcSession();
    return principal;
  } catch (err) {
    if (previous) setToken(previous);
    else clearToken();
    throw err;
  }
}

/**
 * Turn a thrown `login()` failure into a friendly, user-facing message. Splits the
 * three cases a person actually hits at the login form — wrong credentials, an
 * unreachable server, and a server-side fault — and falls back to the raw status +
 * detail for anything else (keeping the detail aids debugging).
 */
export function describeLoginError(err: unknown): string {
  if (err instanceof NetworkError) {
    return "Can't reach the server — check it's running and the server URL is correct.";
  }
  if (err instanceof ApiError) {
    if (err.status === 401) return "Incorrect username or password.";
    if (err.status >= 500) return `Server error (${err.status}): ${err.message}`;
    return `${err.status}: ${err.message}`;
  }
  return String(err);
}

/**
 * Local logout: drop the stored token and any OIDC session sidecar, for either kind
 * of session. For an OIDC session this is only the *local* half — `oidcLogout()`
 * (oidc.ts) additionally ends the IdP session (RP-initiated logout, §10).
 */
export function logout(): void {
  clearToken();
  clearOidcSession();
}

export function me(): Promise<Principal> {
  return request<Principal>("GET", "/auth/me");
}

/** What the server advertises about its auth backends (docs/auth-oidc.md §10). */
export interface ServerAuthConfig {
  /** Built-in username/password is always available; the form is always shown. */
  builtin: boolean;
  /** Present (and `enabled`) only when the deployment turned OIDC on. */
  oidc?: {
    enabled: boolean;
    issuer: string;
    clientId: string;
    /** From the IdP's discovery document; the client may re-discover the rest. */
    authorizationEndpoint?: string;
    scopes?: string[];
  };
}

/**
 * Fetch the server's public auth advertisement (unauthenticated, like `/status`).
 * A UI uses this to decide whether to show the OIDC button; when the server has
 * OIDC off (`oidc` absent or `enabled:false`) the client behaves exactly as today
 * and shows only the username/password form.
 */
export function getAuthConfig(): Promise<ServerAuthConfig> {
  return request<ServerAuthConfig>("GET", "/auth/config", undefined, { auth: false });
}

// ---- household admin: users -------------------------------------------------

/**
 * Admin-only management of the household roster. Every call 403s for a member
 * token; the web/admin UI gates the surface to admins to match.
 */
export const users = {
  list: () => request<User[]>("GET", "/auth/users"),
  create: (username: string, password: string, role: "member" | "admin" = "member") =>
    request<Principal>("POST", "/auth/users", { username, password, role }),
  remove: (id: string) => request<void>("DELETE", `/auth/users/${id}`),
};

// ---- component status -------------------------------------------------------

/**
 * Per-component health for the status view. Public (no auth required), so a
 * client can show whether the server and each backend are reachable even before
 * sign-in. A `NetworkError` here means the api itself is unreachable — the caller
 * should render that as the whole system being down.
 */
export function getStatus(): Promise<SystemStatus> {
  return request<SystemStatus>("GET", "/status");
}

// ---- history ----------------------------------------------------------------

export const history = {
  list: (q?: string, limit = 50, offset = 0) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (q) params.set("q", q);
    return request<ConversationSummary[]>("GET", `/conversations?${params.toString()}`);
  },
  get: (id: string) => request<Conversation>("GET", `/conversations/${id}`),
  remove: (id: string) => request<void>("DELETE", `/conversations/${id}`),
  // Audio is opened by plain navigation (`<a href>` / Linking.openURL), which can't
  // set an Authorization header — so the token rides as `?token=`
  // (the api accepts it there for this endpoint). Without it the download 401s.
  audioUrl: (id: string) => {
    const url = `${apiBaseUrl()}/conversations/${id}/audio`;
    const token = getToken();
    return token ? `${url}?token=${encodeURIComponent(token)}` : url;
  },
};
