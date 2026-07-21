/**
 * Even Hub app configuration.
 *
 * The api URL is now a REQUIRED, user-editable setting (master plan §8.5): the
 * wearer points the app at their own self-hosted instance on the companion page,
 * and the lens reads the same persisted choice at boot. `VITE_API_WS` is only a
 * build-time *seed* for dev / first run — a saved choice always wins.
 *
 * The shared REST client lives in `@tenir/client-core` and reads its base URL from
 * `configureApi`, so we resolve the effective URL here and (re)configure the core.
 */

import { configureApi, httpBaseFromWs } from "@tenir/client-core";
import type { Lang, MicSource } from "@tenir/contract";

import { localStorageServerUrlStore, normalizeWsUrl, resolveWsUrl } from "./state/settings";

const SEED_WS = import.meta.env.VITE_API_WS as string | undefined;
const HTTP_OVERRIDE = import.meta.env.VITE_API_HTTP as string | undefined;
const store = localStorageServerUrlStore();

let currentWsUrl = resolveWsUrl(store.load(), SEED_WS);
// A saved choice drives both URLs; the explicit VITE_API_HTTP override only applies
// while running on the seed (dev proxying), so a user-chosen server stays consistent.
let currentHttpUrl = store.load() ? httpBaseFromWs(currentWsUrl) : (HTTP_OVERRIDE ?? httpBaseFromWs(currentWsUrl));
configureApi({ httpBaseUrl: currentHttpUrl });

export const config = {
  /** Effective api WebSocket endpoint (saved choice → build seed → localhost). */
  get apiWsUrl(): string {
    return currentWsUrl;
  },
  /** Effective api REST base, used by the companion page (sign-in, web-app link). */
  get apiHttpUrl(): string {
    return currentHttpUrl;
  },
  /** Default microphone (glasses by default, phone for seated/table). */
  defaultMicSource: "g2-microphone" as MicSource,
  /** Leave the source language unset so STT auto-detects per turn. */
  defaultSourceLang: undefined as Lang | undefined,
} as const;

/** Whether the user has explicitly configured a server URL (vs. running on the seed). */
export function isServerConfigured(): boolean {
  return normalizeWsUrl(store.load() ?? "") !== null;
}

/**
 * Persist + apply a user-entered server URL, repointing the shared REST client.
 * Returns the resolved ws/http pair, or null when the input isn't a valid ws(s) URL
 * (so the caller can reject it). The lens picks the new URL up on its next launch.
 */
export function applyServerUrl(rawWsUrl: string): { wsUrl: string; httpBaseUrl: string } | null {
  const wsUrl = normalizeWsUrl(rawWsUrl);
  if (!wsUrl) return null;
  store.save(wsUrl);
  currentWsUrl = wsUrl;
  currentHttpUrl = httpBaseFromWs(wsUrl);
  configureApi({ httpBaseUrl: currentHttpUrl });
  return { wsUrl, httpBaseUrl: currentHttpUrl };
}
