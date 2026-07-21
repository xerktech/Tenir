/**
 * Api REST client.
 *
 * Thin typed wrapper over the api's REST surface — auth, users, history/search,
 * and the component status. Every call carries the bearer token via
 * `authHeader()`; a non-2xx response throws an `ApiError` carrying the status and
 * the server's detail message. Shared by every TS frontend.
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

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { ...authHeader() };
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
}

export interface Conversation extends ConversationSummary {
  segments: SegmentView[];
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

export async function login(username: string, password: string): Promise<Principal> {
  const out = await request<{ token: string }>("POST", "/auth/login", { username, password });
  setToken(out.token);
  return me();
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

export function logout(): void {
  clearToken();
}

export function me(): Promise<Principal> {
  return request<Principal>("GET", "/auth/me");
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
