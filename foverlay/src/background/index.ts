/**
 * Background JSContext entry — Tenir miniapp.
 *
 * A local-miniapp port of the Tenir Even G2 glasses app (self-hosted live
 * captions). `registerMiniapp(...)` wires the handler to fire after CONNECT
 * lands; the controller constructed here lives for the entire session and
 * survives WebView open/close cycles. It signs in against the user's
 * self-hosted Tenir server, streams mic PCM over the captions WebSocket,
 * renders the live HUD on the glasses display, and talks to the UI WebView
 * over the typed session.ui channel bus (see shared/channels.ts).
 */

import { registerMiniapp } from "@mentra/miniapp/background";

import { TenirController } from "./controllers/TenirController";

registerMiniapp((session) => {
  void new TenirController(session).start();
});
