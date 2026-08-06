/**
 * Server-URL parsing for setup / sign-in screens (master plan §8.5).
 *
 * Clients point at the user's own self-hosted api, entered by hand. People type
 * URLs loosely — `my-server.com`, `https://my-server.com`, `wss://my-server.com/ws`
 * — so this normalizes any of those into the canonical `ws(s)://host[:port]/ws`
 * form the core expects (`httpBaseFromWs` derives the REST base from it). Shared
 * by the mobile setup screen and the Even G2 phone login page; kept
 * environment-free so it is unit-tested under vitest.
 *
 * Deliberately avoids the WHATWG `URL` class: this module also runs in the
 * miniapp background JSContext (JSC / Zipline-QuickJS), a bare engine where
 * `URL` does not exist. Relying on it there made every login fail with
 * "Enter your server address" (XERK-216), so all parsing is regex/string only.
 */

/** The pieces of an absolute `scheme://host[:port][/path]` URL. */
export interface UrlParts {
  /** Lowercased scheme without the `://`, e.g. `"wss"`. */
  scheme: string;
  /** Lowercased `host[:port]` (IPv6 hosts keep their brackets). */
  host: string;
  /** Path starting with `/`, or `""` when absent. Query/hash are dropped. */
  path: string;
}

/**
 * Split an absolute URL into scheme/host/path without the `URL` class.
 * Returns `null` when the input is not a plausible absolute URL: no
 * `scheme://`, an empty or whitespace-containing host, or a non-numeric port.
 * Userinfo (`user:pw@`) is stripped from the host like `URL` does; query and
 * hash are dropped.
 */
export function splitUrl(input: string): UrlParts | null {
  const m = /^([a-z][a-z0-9+.-]*):\/\/([^/?#]*)([^?#]*)/i.exec(input);
  if (!m) return null;
  const host = m[2].replace(/^[^@]*@/, "").toLowerCase();
  if (!host || /\s/.test(host)) return null;
  // Port sanity — `[v6]:port` or `host:port` must carry a numeric port.
  const wellFormed = host.startsWith("[")
    ? /^\[[^\]]+\](?::\d+)?$/.test(host)
    : /^[^:]+(?::\d+)?$/.test(host);
  if (!wellFormed) return null;
  return { scheme: m[1].toLowerCase(), host, path: m[3] };
}

/**
 * Normalize a user-entered server URL into a `ws://`/`wss://` URL ending in a path.
 *
 * - `http(s)://` is rewritten to `ws(s)://` (the websocket equivalents).
 * - A bare host (no scheme) defaults to the secure `wss://` scheme.
 * - A missing/`"/"` path defaults to `/ws` (the api websocket endpoint); an explicit
 *   path is preserved.
 *
 * Returns `""` when the input is empty or cannot be parsed as a URL.
 */
export function normalizeServerUrl(input: string): string {
  let s = input.trim();
  if (!s) return "";
  s = s.replace(/^http:\/\//i, "ws://").replace(/^https:\/\//i, "wss://");
  if (!/^wss?:\/\//i.test(s)) s = `wss://${s}`;
  const parts = splitUrl(s);
  if (!parts || (parts.scheme !== "ws" && parts.scheme !== "wss")) return "";
  const path = parts.path === "" || parts.path === "/" ? "/ws" : parts.path;
  return `${parts.scheme}://${parts.host}${path}`;
}

/** Whether the input normalizes to a usable `ws(s)://host…` server URL. */
export function isValidServerUrl(input: string): boolean {
  return normalizeServerUrl(input) !== "";
}

/**
 * Render a server URL in the friendly, host-only form people actually type — the
 * inverse of {@link normalizeServerUrl} for display. Strips the `ws(s)://` scheme
 * and the default `/ws` endpoint path so `wss://tenir.example.com/ws` shows as
 * `tenir.example.com`, while a non-default port or path is preserved so the value
 * still round-trips back through `normalizeServerUrl` (e.g.
 * `ws://localhost:8080/ws` → `localhost:8080`, `wss://host/api/ws` → `host/api/ws`).
 *
 * Used to seed the setup / settings server field so the user sees `tenir.example.com`
 * rather than the internal `wss://…/ws` form. Returns the trimmed input unchanged
 * when it can't be parsed as a URL (it's already a bare host).
 */
export function displayServerUrl(input: string): string {
  const s = input.trim();
  if (!s) return "";
  const parts = splitUrl(s);
  if (!parts) return s;
  const path =
    parts.path === "/ws" || parts.path === "/" || parts.path === ""
      ? ""
      : parts.path.replace(/\/$/, "");
  return `${parts.host}${path}`;
}
