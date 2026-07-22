/**
 * Api server-URL setting for the Even Hub app (master plan §8.5).
 *
 * The Even G2 app used to bake its api URL in at build time. It now carries a
 * REQUIRED, user-editable server setting like the web/mobile clients: the wearer
 * points the app at their own self-hosted instance on the companion page, and the
 * lens picks the same URL up at boot. The chosen URL is persisted in `localStorage`,
 * which the off-lens companion page and the on-lens app share (same Even Hub WebView
 * origin) — the same store `client-core` uses for the bearer token.
 *
 * The build-time `VITE_API_WS` becomes a *seed* (a sensible default for dev / first
 * run), not the source of truth: a saved choice always wins. Everything here is pure
 * or storage-only so it's unit-tested without the Even SDK.
 */

import type { CueLevel } from "@tenir/contract";

export const SERVER_URL_KEY = "tenir.serverUrl";
export const CUE_LEVEL_KEY = "tenir.cueLevel";
export const CUE_LEVELS: CueLevel[] = ["conservative", "balanced", "aggressive"];
const DEFAULT_CUE_LEVEL: CueLevel = "balanced";

/**
 * Load the wearer's cue aggressiveness (XERK-81), sent on session.start and shared
 * with the companion page via the same localStorage the server URL uses. Defaults
 * to balanced; an unrecognized stored value falls back to the default.
 */
export function loadCueLevel(): CueLevel {
  let v: string | null = null;
  try {
    v = localStorage.getItem(CUE_LEVEL_KEY);
  } catch {
    v = null;
  }
  return (CUE_LEVELS as string[]).includes(v ?? "") ? (v as CueLevel) : DEFAULT_CUE_LEVEL;
}

/** Persist the wearer's chosen cue level (best-effort). */
export function saveCueLevel(level: CueLevel): void {
  try {
    localStorage.setItem(CUE_LEVEL_KEY, level);
  } catch {
    /* storage unavailable — the choice just won't persist */
  }
}

/** Last-resort default when nothing valid is configured (local dev api). */
export const FALLBACK_WS_URL = "ws://localhost:8080/ws";

/**
 * Validate + normalize a user-entered WS URL. Returns the cleaned `ws://`/`wss://`
 * URL (trimmed, no trailing slash), or null when it's empty or not a ws(s) URL — so
 * callers can reject a bad value instead of persisting it.
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

/** A synchronous server-URL store (localStorage in the app; in-memory under tests). */
export interface ServerUrlStore {
  /** The saved WS URL, or null when the user hasn't configured one yet. */
  load(): string | null;
  /** Persist the chosen WS URL. */
  save(wsUrl: string): void;
  /** Forget the saved URL. */
  clear(): void;
}

/**
 * `localStorage`-backed store, shared by the lens app and the companion page.
 * Storage failures are swallowed (a dropped read/write just means we fall back to
 * the seed), mirroring `client-core`'s token store.
 */
export function localStorageServerUrlStore(): ServerUrlStore {
  return {
    load() {
      try {
        return localStorage.getItem(SERVER_URL_KEY);
      } catch {
        return null;
      }
    },
    save(wsUrl) {
      try {
        localStorage.setItem(SERVER_URL_KEY, wsUrl);
      } catch {
        /* storage unavailable — the choice just won't persist */
      }
    },
    clear() {
      try {
        localStorage.removeItem(SERVER_URL_KEY);
      } catch {
        /* ignore */
      }
    },
  };
}
