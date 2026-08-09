/**
 * Api REST client.
 *
 * Ported from Tenir's `packages/client-core/src/api.ts` — a thin typed wrapper
 * over the api's REST surface (auth + history). Every call carries the bearer
 * token via `authHeader()`; a non-2xx response throws an `ApiError` carrying
 * the status and the server's detail message. Runs in the miniapp background
 * JSContext (no CORS), where the polyfilled `fetch` supports string bodies —
 * which is all this client ever sends.
 *
 * Not ported: the household-admin `users` roster and `getStatus`.
 */

import { authHeader, clearToken, getToken, setToken } from "./auth";
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
 * A transport-level failure: the request never reached the api (server down,
 * DNS failure, offline). Distinct from `ApiError`, which means the server
 * answered with a non-2xx status. Carries the underlying cause for logs.
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
 * One authenticated request. Exported (upstream kept it private) so the
 * controller's proxied-fetch RPC can reuse the exact same path — including the
 * sliding-token renewal below — for arbitrary history endpoints.
 */
export interface RequestOptions {
  /**
   * Send this request to an explicit base instead of the configured one, WITHOUT
   * touching the global (XERK-237). `configureApi` is a module-level singleton
   * every other handler reads, so pointing it at a candidate server for the
   * duration of a login left a window — as long as that server cared to keep the
   * request open — in which unrelated handlers minted token-bearing URLs against
   * it. Passing the base explicitly means nothing shared moves until the server
   * has actually been accepted.
   */
  baseUrl?: string;
  /** Send the bearer token. Default true; `/auth/login` opts out (see `login`). */
  auth?: boolean;
}

export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = opts.auth === false ? {} : { ...authHeader() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`${opts.baseUrl ?? apiBaseUrl()}${path}`, {
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
  // Sliding renewal (XERK-168): past half a token's life the api attaches a
  // fresh one to every authenticated response. Adopting it here — the one
  // request path — is what keeps a device logged in until it explicitly logs
  // out, instead of being bounced to the login screen when the token expires.
  // Only from a response the server actually accepted (XERK-237). Adopting it
  // off any response let a REJECTED request replace the wearer's live token —
  // so a failed login against a server the page named came back with a
  // replacement the device stored, leaving the real server 401ing.
  if (res.ok) {
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

// ---- auth -------------------------------------------------------------------

/**
 * Sign in, optionally against an explicit base rather than the configured one
 * (XERK-237) — so a login attempt at a server the user has just typed doesn't
 * have to move global state that every other handler reads.
 *
 * The credentials are the ONLY thing sent: the bearer token is deliberately
 * withheld (`auth: false`). A login carries no authority worth proving, and
 * attaching it handed the wearer's live token to whatever server was named —
 * which is what made a mistyped or hostile address a token leak rather than
 * just a failed sign-in.
 */
export async function login(
  username: string,
  password: string,
  baseUrl?: string,
): Promise<Principal> {
  const out = await request<{ token: string }>(
    "POST",
    "/auth/login",
    { username, password },
    { baseUrl, auth: false },
  );
  // Two round-trips, so the new token is only PROVISIONAL until `me()` confirms
  // it (XERK-237). Storing it unconditionally meant a server that accepted the
  // login and then rejected `/auth/me` destroyed the wearer's existing token on
  // an attempt that reports failure — leaving the real server 401ing until a
  // silent re-login healed it. Put the previous one back if the confirmation
  // doesn't come.
  const previous = getToken();
  setToken(out.token);
  try {
    return await me(baseUrl);
  } catch (err) {
    if (previous) setToken(previous);
    else clearToken();
    throw err;
  }
}

/**
 * Turn a thrown `login()` failure into a friendly, user-facing message. Splits
 * the three cases a person actually hits at the login form — wrong credentials,
 * an unreachable server, and a server-side fault — and falls back to the raw
 * status + detail for anything else (keeping the detail aids debugging).
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

export function logout(): void {
  clearToken();
}

export function me(baseUrl?: string): Promise<Principal> {
  return request<Principal>("GET", "/auth/me", undefined, { baseUrl });
}

// ---- history ----------------------------------------------------------------

export const history = {
  list: (q?: string, limit = 50, offset = 0) => {
    // Hand-built query string — the background JSContext has no URLSearchParams
    // (same bare-engine constraint as serverUrl.ts, XERK-216).
    let params = `limit=${limit}&offset=${offset}`;
    if (q) params += `&q=${encodeURIComponent(q)}`;
    return request<ConversationSummary[]>("GET", `/conversations?${params}`);
  },
  get: (id: string) => request<Conversation>("GET", `/conversations/${id}`),
  remove: (id: string) => request<void>("DELETE", `/conversations/${id}`),
  /**
   * The retained clip's URL (upstream `client-core`'s `history.audioUrl`).
   * Audio is opened by plain navigation — an `<audio src>` or the host's
   * download sheet — neither of which can set an Authorization header, so the
   * token rides as `?token=` (the api accepts it there for this endpoint).
   * Without it the request 401s.
   */
  audioUrl: (id: string) => {
    // The id is encoded as a path segment: this URL carries the bearer token,
    // so a "../"-shaped id must not be able to point it somewhere else.
    const url = `${apiBaseUrl()}/conversations/${encodeURIComponent(id)}/audio`;
    const token = getToken();
    return token ? `${url}?token=${encodeURIComponent(token)}` : url;
  },
};
