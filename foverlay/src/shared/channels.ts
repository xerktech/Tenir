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
  /** Open a URL in the system browser (e.g. the server's web app). */
  "tenir:open-url": { url: string };
}

declare global {
  // eslint-disable-next-line no-var
  var mentra: import("@mentra/miniapp/ui").MentraTyped<Channels>;
}
