/**
 * Api endpoint configuration for the shared client core.
 *
 * `client-core` is consumed by several frontends (the Even G2 glasses app, the web
 * SPA, and the mobile app), each of which discovers the api differently: the
 * Even G2 app bakes it in at build time via Vite env, while the web/mobile clients
 * let the user point at *their own* self-hosted instance with a server-URL field
 * (master plan §8.5). So the base URL is injected by the host rather than read
 * from `import.meta.env` here, keeping the core environment-agnostic.
 */

const DEFAULT_HTTP_BASE = "http://localhost:8080";

let httpBaseUrl = DEFAULT_HTTP_BASE;

/** Point the REST client at a api. Call once at startup (and on server-URL change). */
export function configureApi(opts: { httpBaseUrl: string }): void {
  httpBaseUrl = opts.httpBaseUrl.replace(/\/$/, "");
}

/** The configured REST base, used by the api client to build request URLs. */
export function apiBaseUrl(): string {
  return httpBaseUrl;
}

/** Derive the REST base from a WS URL (ws→http, drop a trailing /ws path). */
export function httpBaseFromWs(wsUrl: string): string {
  try {
    const u = new URL(wsUrl);
    u.protocol = u.protocol === "wss:" ? "https:" : "http:";
    u.pathname = u.pathname.replace(/\/ws$/, "");
    u.search = "";
    return u.toString().replace(/\/$/, "");
  } catch {
    return DEFAULT_HTTP_BASE;
  }
}

/** Derive the WS URL from a REST base (http→ws, append /ws). */
export function wsFromHttpBase(httpBase: string): string {
  try {
    const u = new URL(httpBase);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.pathname = `${u.pathname.replace(/\/$/, "")}/ws`;
    u.search = "";
    return u.toString();
  } catch {
    return "ws://localhost:8080/ws";
  }
}
