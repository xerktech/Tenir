/**
 * Server-URL parsing for the initial setup screen (master plan §8.5).
 *
 * The normalization lives in `@tenir/client-core` (`serverUrl.ts`); this
 * module re-exports it to keep the mobile import paths (and their tests, which
 * exercise the React Native URL polyfill) unchanged.
 */

export { displayServerUrl, isValidServerUrl, normalizeServerUrl } from "@tenir/client-core";
