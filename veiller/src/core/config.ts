/**
 * Api endpoint configuration for the shared client core.
 *
 * `client-core` is consumed by several frontends (the Even G2 glasses app, the web
 * SPA, and the mobile app), each of which discovers the api differently: the
 * Even G2 app bakes it in at build time via Vite env, while the web/mobile clients
 * let the user point at *their own* self-hosted instance with a server-URL field
 * (master plan §8.5). So the base URL is injected by the host rather than read
 * from `import.meta.env` here, keeping the core environment-agnostic.
 *
 * Like `serverUrl.ts`, the derivations avoid the WHATWG `URL` class: they run
 * in the miniapp background JSContext (JSC / Zipline-QuickJS) where `URL`
 * doesn't exist, and the silent `catch` fallbacks here would misroute every
 * request to localhost (XERK-216).
 */

import { splitUrl } from "./serverUrl";

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
  const parts = splitUrl(wsUrl.trim());
  if (!parts) return DEFAULT_HTTP_BASE;
  const scheme = parts.scheme === "wss" ? "https" : "http";
  const path = parts.path.replace(/\/ws$/, "").replace(/\/$/, "");
  return `${scheme}://${parts.host}${path}`;
}

/** Derive the WS URL from a REST base (http→ws, append /ws). */
export function wsFromHttpBase(httpBase: string): string {
  const parts = splitUrl(httpBase.trim());
  if (!parts) return "ws://localhost:8080/ws";
  const scheme = parts.scheme === "https" ? "wss" : "ws";
  return `${scheme}://${parts.host}${parts.path.replace(/\/$/, "")}/ws`;
}
