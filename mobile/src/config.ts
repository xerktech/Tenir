/**
 * Mobile app api configuration (master plan §8.5).
 *
 * Unlike the Even G2 app (`even/`), which ships pointed at a known instance, the
 * mobile client lets the user pick their own server at runtime — the same posture as
 * the web SPA. This helper points the shared REST client at a user-supplied api;
 * the chosen URL is persisted via `storage.ts` and re-applied on launch in `bootstrap.ts`.
 */

import { configureApi, httpBaseFromWs } from "@tenir/client-core";
import type { Lang, MicSource } from "@tenir/contract";

/** Seed URL shown in the server field on first launch (a local dev api). */
export const DEFAULT_WS_URL = "ws://localhost:8080/ws";

/**
 * The phone is the capture device here, so live sessions default to the phone mic
 * (the Even G2 app defaults to the glasses mic — decision #5). The contract still
 * allows switching to `g2-microphone` at runtime when paired glasses are present.
 */
export const DEFAULT_MIC_SOURCE: MicSource = "phone-microphone";

/**
 * Leave the source language unset so STT auto-detects per turn and the server
 * auto-translates non-reading-language turns with no client toggle (master plan §5.4,
 * decision #8). v1 detects among EN/ES.
 */
export const DEFAULT_SOURCE_LANG: Lang | undefined = undefined;

/** Point the shared client-core REST client at the api behind `wsUrl`. */
export function configureApiFromWs(wsUrl: string): { wsUrl: string; httpBaseUrl: string } {
  const httpBaseUrl = httpBaseFromWs(wsUrl);
  configureApi({ httpBaseUrl });
  return { wsUrl, httpBaseUrl };
}
