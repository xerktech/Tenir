/**
 * Mobile app api configuration (master plan §8.5).
 *
 * The mobile client lets the user pick their own server at runtime — the same
 * posture as the web SPA. This helper points the shared REST client at a user-supplied api;
 * the chosen URL is persisted via `storage.ts` and re-applied on launch in `bootstrap.ts`.
 */

import { configureApi, httpBaseFromWs } from "@tenir/client-core";
import type { Lang, MicSource } from "@tenir/contract";

/** Seed URL shown in the server field on first launch (a local dev api). */
export const DEFAULT_WS_URL = "ws://localhost:8080/ws";

/**
 * The phone is the capture device here, so live sessions use the phone mic.
 */
export const DEFAULT_MIC_SOURCE: MicSource = "phone-microphone";

/**
 * Leave the source language unset so STT auto-detects per turn and the server
 * auto-translates non-English turns with no client toggle (master plan §5.4,
 * decision #8; shipped as live translations, XERK-160): each non-English
 * finalized turn arrives paired with an English `translation` the Live screen
 * renders under the original.
 */
export const DEFAULT_SOURCE_LANG: Lang | undefined = undefined;

/** Point the shared client-core REST client at the api behind `wsUrl`. */
export function configureApiFromWs(wsUrl: string): { wsUrl: string; httpBaseUrl: string } {
  const httpBaseUrl = httpBaseFromWs(wsUrl);
  configureApi({ httpBaseUrl });
  return { wsUrl, httpBaseUrl };
}
