/**
 * Typed channel registry — the single source of truth for every name that
 * flows between this miniapp's background JSContext and its UI WebView. Both
 * halves import this file at build time; the bundler inlines the declarations
 * so there's no runtime cross-boundary I/O.
 *
 * Channels wrapped in `Rpc<Req, Res>` are request/response (call via
 * `mentra.request` / `session.ui.handle`); the rest are broadcast
 * (`mentra.send` + `mentra.on` / `session.ui.send` + `session.ui.on`).
 */

import type { Rpc } from "@mentra/miniapp/ui";

import type {
  LoginRequest,
  LoginResult,
  ProxyFetchRequest,
  ProxyFetchResult,
  StartResult,
  TenirAuthState,
  TenirLiveState,
  TenirSnapshot,
} from "./types";

export interface Channels {
  // ── background → UI ────────────────────────────────────────────────────

  /** Full hydration snapshot, sent on every session.ui.onOpen. */
  "tenir:snapshot": TenirSnapshot;
  /** Auth state change (sign-in / sign-out / server URL applied). */
  "tenir:auth": TenirAuthState;
  /** Live session mirror update (captions, connection, cues, song). */
  "tenir:live": TenirLiveState;
  /**
   * The host's colour scheme (XERK-237). Upstream's phone page follows
   * `prefers-color-scheme`; this WebView is told by the host instead, so the
   * background forwards it here and on every change.
   */
  "tenir:color-scheme": { scheme: "light" | "dark" };

  // ── UI → background ────────────────────────────────────────────────────

  /** Normalize + persist the server URL, POST /auth/login, cache credentials. */
  "tenir:login": Rpc<LoginRequest, LoginResult>;
  /** Clear token + cached credentials; the lens shows its sign-in prompt. */
  "tenir:logout": Rpc<Record<string, never>, { ok: true }>;
  /** Start a capture session (same transition a lens tap drives). */
  "tenir:start": Rpc<Record<string, never>, StartResult>;
  /** Stop the running capture session. */
  "tenir:stop": Rpc<Record<string, never>, { ok: boolean }>;
  /** Proxied authenticated REST call (history list/detail/delete). */
  "tenir:fetch": Rpc<ProxyFetchRequest, ProxyFetchResult>;
  /**
   * The playable URL of a conversation's retained audio (XERK-237): the api
   * endpoint with the bearer token as a query param, which is how upstream's
   * `<audio src>` / download link reach it too. Minted on demand rather than
   * broadcast, so the token isn't carried in every live update.
   */
  "tenir:audio-url": Rpc<{ id: string }, { ok: boolean; url?: string }>;
  /**
   * Save a conversation's retained clip through the host's download sheet.
   *
   * Takes the conversation id, NOT a URL: the background mints the URL itself,
   * so the page can never name the target. The host's download sheet does not
   * scheme-filter the way `openUrl` does, and the URL carries the wearer's
   * bearer token — a URL argument would be arbitrary network/file egress with
   * the token attached, and no allow-list can be anchored safely while the page
   * can also re-point the api base through `tenir:login`.
   */
  "tenir:download": Rpc<{ id: string }, { ok: boolean }>;
}

declare global {
  // eslint-disable-next-line no-var
  var mentra: import("@mentra/miniapp/ui").MentraTyped<Channels>;
}
