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
  getSessionKind,
  getToken,
  noteNewSignIn,
  lastOwnRefreshedToken,
  setToken,
  tokenGenerations,
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

/**
 * The api answered 503: it is up but can't reach its database (XERK-1510). A
 * `NetworkError` on purpose — like an unreachable server it is transient and says
 * nothing about the token, so every client keeps the session and retries rather
 * than dropping to a login form; `describeLoginError` still tells it apart.
 */
export class ServerUnavailableError extends NetworkError {
  constructor(message: string) {
    super(message);
    this.name = "ServerUnavailableError";
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
  /**
   * Present this bearer token instead of the stored one — a token not yet adopted
   * (see `login`). Never refreshed or retried, and its renewal header is ignored:
   * it is not the session's token.
   */
  token?: string;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: RequestOptions = {},
): Promise<T> {
  const presented = opts.token !== undefined;
  const headers: Record<string, string> =
    opts.auth === false
      ? {}
      : presented
        ? { Authorization: `Bearer ${opts.token}` }
        : { ...authHeader() };
  const sentGen = tokenGenerations();
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
  //
  // XERK-1591:
  // - If the sign-in changed while this request was in flight (logout, a new login),
  //   neither retry nor refresh: the 401 belongs to a session that no longer exists,
  //   and replaying it would send one user's request as another's.
  // - A 401 for a token that a refresh has since replaced (a concurrent request's
  //   refresh landed meanwhile) just retries with the current token — a second
  //   refresh would reuse a rotated refresh token.
  if (res.status === 401 && opts.auth !== false && !opts.retried && !presented) {
    const gen = tokenGenerations();
    const sameSignIn = gen.epoch === sentGen.epoch;
    if (sameSignIn && (gen.refresh !== sentGen.refresh || (await tryOidcRefresh()))) {
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
  // Only for the session's own token, and only if the sign-in hasn't changed since
  // the request went out — a renewal of the previous user's token must not replace
  // the new sign-in's (XERK-1591).
  if (opts.auth !== false && !presented && tokenGenerations().epoch === sentGen.epoch) {
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
    if (res.status === 503) throw new ServerUnavailableError(detail);
    // 502/504: a gateway in front of the api answered for it (the api is gone or
    // not answering) — unreachable, not a verdict on the token.
    if (res.status === 502 || res.status === 504) throw new NetworkError(detail);
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

/**
 * A paired screen-capture device (XERK-2594): one live device token, listed without the
 * token itself. Revoking it cuts that device off without logging anything else out.
 */
export interface Device {
  id: string;
  /** The capture PC's hostname, or "Screen capture" when it sent none. */
  name: string;
  /** ISO 8601 timestamps. */
  createdAt: string;
  expiresAt: string;
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
  /** "mic" for a live audio session, "screen" for a meeting read off captured captions
   *  (XERK-2505). Absent on payloads from an older server. */
  source?: string;
  /** Screen meetings only: the org the window belongs to, the platform, the title. */
  orgSiteKey?: string | null;
  orgName?: string | null;
  platform?: string | null;
  meetingTitle?: string | null;
}

/** An org the caller has screen-captured meetings from — the history org filter. */
export interface OrgView {
  siteKey: string;
  name: string;
}

export interface SegmentView {
  segmentId: string;
  text: string;
  startMs: number;
  endMs: number;
  lang: string | null;
  /** English translation of a non-English turn (XERK-160); absent/null otherwise. */
  translation?: string | null;
  /** Who said it, on a screen-captured meeting (XERK-2505); null otherwise. */
  speaker?: string | null;
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
  /** Participant tile names of a screen meeting, first seen first (XERK-2505). */
  participants?: string[];
}

const PLATFORM_LABELS: Record<string, string> = { zoom: "Zoom" };

/** Display name of a meeting platform id ("zoom" -> "Zoom"); unknown ids pass through. */
export function platformLabel(platform: string): string {
  return PLATFORM_LABELS[platform] ?? platform;
}

/** Everyone heard or seen in a meeting (XERK-2507): the participant tiles, then any caption
 *  speaker not among them, each once, in first-seen order. */
export function meetingSpeakers(conv: Conversation): string[] {
  const names = [...(conv.participants ?? [])];
  for (const seg of conv.segments) if (seg.speaker) names.push(seg.speaker);
  return [...new Set(names)];
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
  // Confirm the new token by presenting it explicitly, without adopting it first
  // (XERK-1591): the stored session — and any OIDC refresh of it — is untouched until
  // the new sign-in is confirmed, a 401 here can't trigger a refresh of the previous
  // session (and sign in as that user), and nothing needs restoring on failure.
  const principal = await request<Principal>("GET", "/auth/me", undefined, {
    token: out.token,
  });
  // A new sign-in: work in flight under the previous one must not adopt this token,
  // and a refresh of the previous session must not overwrite it.
  noteNewSignIn();
  setToken(out.token);
  // A confirmed built-in login supersedes any prior OIDC session, so the session
  // kind reverts to built-in (no more silent-refresh attempts on this token).
  clearOidcSession();
  return principal;
}

/**
 * Turn a thrown `login()` failure into a friendly, user-facing message. Splits the
 * three cases a person actually hits at the login form — wrong credentials, an
 * unreachable server, and a server-side fault — and falls back to the raw status +
 * detail for anything else (keeping the detail aids debugging).
 */
export function describeLoginError(err: unknown): string {
  if (err instanceof ServerUnavailableError) {
    return "The server can't reach its database right now — try again shortly.";
  }
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
 * Heading + body for the boot state where the stored session couldn't be checked
 * (`checkSession` rejected). Shared so web and Android word it identically: a 5xx
 * answer gets server-error copy, anything else (unreachable, database down) gets
 * the unreachable copy. The detail line comes from `describeLoginError`.
 */
export function describeBootFailure(err: unknown): { heading: string; body: string } {
  const lead = "We couldn't check whether you're still signed in.";
  if (err instanceof ApiError) {
    return {
      heading: "Your server hit an error",
      body: `${lead} Your Tenir server answered with an error, which doesn't mean you were signed out — retry in a moment.`,
    };
  }
  return {
    heading: "Can't reach your server",
    body: `${lead} If your Tenir server is just offline or restarting, you don't need to sign in again — retry once it's back.`,
  };
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

/**
 * The failure is the server's, not the token's: unreachable (`NetworkError`,
 * including 502/503/504) or any other 5xx. Says nothing about whether the stored
 * session is still valid. Deliberately NOT folded into `request()`: a 500 on an
 * ordinary action must still surface as an `ApiError` the caller reports.
 */
export function isServerFault(err: unknown): boolean {
  return err instanceof NetworkError || (err instanceof ApiError && err.status >= 500);
}

/**
 * Confirm the stored session at app boot (web + Android share this; XERK-1591).
 *
 * - Resolves the principal when the token is good.
 * - A 401 means the token is dead: clear it and resolve null, so a later outage
 *   doesn't tell this user they're still signed in (an OIDC refresh sidecar is kept).
 * - A server fault (`isServerFault`) with a stored token rejects with that error:
 *   the token may be fine, so keep it and let the caller show an unreachable /
 *   server-error state with Retry instead of a login form.
 * - Anything else (or a server fault with no stored token — a first run) resolves
 *   null: show the login form.
 */
export async function checkSession(): Promise<Principal | null> {
  return checkSessionOnce(true);
}

async function checkSessionOnce(mayRecheck: boolean): Promise<Principal | null> {
  const checked = getToken();
  const sentGen = tokenGenerations();
  try {
    return await me();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      // Compare-and-clear (XERK-1591): only the token this check proved dead — the one
      // it sent, or the one this context's own refresh stored and the retry sent. If
      // another tab replaced the token meanwhile (its refresh, a new sign-in), that
      // token is unchecked and must survive: check it instead, once.
      const now = getToken();
      const ownRefresh =
        tokenGenerations().refresh !== sentGen.refresh && now === lastOwnRefreshedToken();
      if (now !== checked && !ownRefresh) {
        return mayRecheck && now !== null ? checkSessionOnce(false) : null;
      }
      // An OIDC session keeps its refresh sidecar: `request()` already tried a silent
      // refresh, and it may have failed only because the IdP is briefly unreachable.
      // With the access token gone, the next `me()` 401s and refreshes again, so the
      // session recovers once the IdP is back; a built-in token has nothing to keep.
      if (getSessionKind() === "oidc") clearToken();
      else logout();
      return null;
    }
    if (isServerFault(err) && getToken() != null) throw err;
    return null;
  }
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

// ---- screen-capture devices ---------------------------------------------------

/**
 * The signed-in user's own paired screen-capture devices (XERK-2594). Pairing happens on
 * the capture PC (`tenir-capture --pair`); the web and Android Devices views list and
 * revoke. A device token itself can't call these.
 */
export const devices = {
  list: () => request<Device[]>("GET", "/auth/device-tokens"),
  revoke: (id: string) => request<void>("DELETE", `/auth/device-tokens/${encodeURIComponent(id)}`),
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
  /** `org` keeps only screen meetings from that org site key (XERK-2507). */
  list: (q?: string, limit = 50, offset = 0, org?: string) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (q) params.set("q", q);
    if (org) params.set("org", org);
    return request<ConversationSummary[]>("GET", `/conversations?${params.toString()}`);
  },
  /** Orgs the caller has meetings from, by name — the options of the org filter. */
  orgs: () => request<OrgView[]>("GET", "/conversations/orgs"),
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
