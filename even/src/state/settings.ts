/**
 * Api server-URL setting for the Even Hub app (master plan §8.5).
 *
 * The api URL is a REQUIRED, user-editable setting like the web/mobile clients:
 * the wearer points the app at their own self-hosted instance on the phone login
 * page, and the lens picks the same URL up at boot. The chosen URL is persisted
 * through `KeyValueStorage` — bridge-backed on device (XERK-82: browser
 * `localStorage` does NOT survive app restarts in this host, so persisting there
 * meant re-entering the URL every launch), `window.localStorage` in plain-browser
 * dev.
 *
 * The build-time `VITE_API_WS` is a *seed* (a sensible default for dev / first
 * run), not the source of truth: a saved choice always wins. Everything here is
 * pure or storage-only so it's unit-tested without the Even SDK.
 */

import type { KeyValueStorage } from "./storage";

export const SERVER_URL_KEY = "tenir.serverUrl";

/** Last-resort default when nothing valid is configured (local dev api). */
export const FALLBACK_WS_URL = "ws://localhost:8080/ws";

/**
 * Validate + normalize an already-canonical WS URL. Returns the cleaned
 * `ws://`/`wss://` URL (trimmed, no trailing slash), or null when it's empty or
 * not a ws(s) URL — so callers can reject a bad value instead of persisting it.
 * (Loose user input — bare hosts, http(s) — goes through client-core's
 * `normalizeServerUrl` first; this guards persisted/seed values.)
 */
export function normalizeWsUrl(input: string): string | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    return null;
  }
  if (url.protocol !== "ws:" && url.protocol !== "wss:") return null;
  return url.toString().replace(/\/$/, "");
}

/**
 * Resolve the effective WS URL by precedence: a saved choice wins, then the
 * build-time seed, then the hard fallback. Each candidate is validated, so a
 * malformed seed or persisted value is skipped rather than used.
 */
export function resolveWsUrl(
  persisted: string | null,
  seed?: string | null,
  fallback: string = FALLBACK_WS_URL,
): string {
  return (
    normalizeWsUrl(persisted ?? "") ??
    normalizeWsUrl(seed ?? "") ??
    normalizeWsUrl(fallback) ??
    FALLBACK_WS_URL
  );
}

/** The saved WS URL, or null when the user hasn't configured a valid one yet. */
export async function loadServerUrl(storage: KeyValueStorage): Promise<string | null> {
  const raw = await storage.get(SERVER_URL_KEY);
  return normalizeWsUrl(raw ?? "");
}

/** Persist the chosen WS URL. */
export async function saveServerUrl(storage: KeyValueStorage, wsUrl: string): Promise<void> {
  await storage.set(SERVER_URL_KEY, wsUrl);
}
