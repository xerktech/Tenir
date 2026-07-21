/**
 * tenir mobile app (master plan §8.1).
 *
 * The phone client — live speech-to-text capture plus browse of recorded sessions
 * (history/search, retained audio) built on the shared `@tenir/client-core`, like
 * the Even G2 app (`even/`) and the web SPA (`web/`). The native entrypoint is
 * `index.js`; this module re-exports the app root and the wiring helpers.
 */

export { App } from "./App";
export { bootstrap } from "./bootstrap";
export { configureApiFromWs, DEFAULT_WS_URL } from "./config";
export { DISCLOSURES, DISCLOSURE_SUMMARY } from "@tenir/client-core";
