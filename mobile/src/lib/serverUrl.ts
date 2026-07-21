/**
 * Server-URL parsing for the initial setup screen (master plan §8.5).
 *
 * The mobile client points at the user's own self-hosted api, entered by hand on the
 * setup screen. People type URLs loosely — `my-server.com`, `https://my-server.com`,
 * `wss://my-server.com/ws` — so this normalizes any of those into the canonical
 * `ws(s)://host[:port]/ws` form `client-core` expects (`httpBaseFromWs` derives the
 * REST base from it). Kept React-Native-free so it is unit-tested under vitest.
 */

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
  try {
    const u = new URL(s);
    if (!u.host) return "";
    if (u.pathname === "" || u.pathname === "/") u.pathname = "/ws";
    u.search = "";
    u.hash = "";
    return u.toString();
  } catch {
    return "";
  }
}

/** Whether the input normalizes to a usable `ws(s)://host…` server URL. */
export function isValidServerUrl(input: string): boolean {
  return normalizeServerUrl(input) !== "";
}
