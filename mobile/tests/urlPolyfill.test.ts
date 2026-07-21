import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { isValidServerUrl, normalizeServerUrl } from "../src/lib/serverUrl";

/**
 * Regression cover for the "sign-in button does nothing" bug (XERK-57 follow-up).
 *
 * React Native 0.76 ships a built-in `URL` whose component getters are stubs that
 * *throw* — `get host()`, `get pathname()`, `get protocol()`, `get search()` all do
 * `throw new Error("URL.host is not implemented")` (see
 * `react-native/Libraries/Blob/URL.js`). `normalizeServerUrl` reads `url.host` /
 * `url.pathname`, so on device every input threw, got swallowed by its try/catch, and
 * returned `""`. That left `isValidServerUrl` false, so the setup screen's
 * "Connect & sign in" button stayed permanently disabled and tapping it did nothing —
 * while the vitest suite passed on Node's spec-compliant `URL`. It also broke the
 * api-base derivation (`httpBaseFromWs`) and history search (`URLSearchParams`).
 *
 * The fix installs `react-native-url-polyfill` at the app entrypoint so a
 * spec-compliant `URL`/`URLSearchParams` is in place before any `client-core` code
 * runs. These tests pin both the on-device failure mode and that the entrypoint loads
 * the polyfill first.
 */

/** A stand-in for React Native's built-in `URL`: constructs, but every getter throws. */
class ThrowingURL {
  constructor(readonly _url: string) {}
  get protocol(): string {
    throw new Error("URL.protocol is not implemented");
  }
  get host(): string {
    throw new Error("URL.host is not implemented");
  }
  get pathname(): string {
    throw new Error("URL.pathname is not implemented");
  }
  get search(): string {
    throw new Error("URL.search is not implemented");
  }
  set search(_value: string) {
    throw new Error("URL.search is not implemented");
  }
  get hash(): string {
    throw new Error("URL.hash is not implemented");
  }
  set hash(_value: string) {
    throw new Error("URL.hash is not implemented");
  }
  toString(): string {
    return this._url;
  }
}

describe("normalizeServerUrl and React Native's URL", () => {
  const realURL = globalThis.URL;
  afterEach(() => {
    globalThis.URL = realURL;
  });

  it("regresses to an unusable (disabled-button) state when URL getters throw", () => {
    // Mimic React Native 0.76's built-in URL: swap in getters that throw.
    globalThis.URL = ThrowingURL as unknown as typeof URL;
    expect(normalizeServerUrl("tenir.example.com")).toBe("");
    expect(isValidServerUrl("tenir.example.com")).toBe(false);
  });

  it("works against a spec-compliant URL — what the polyfill installs on device", () => {
    // realURL is Node's WHATWG URL, the same shape react-native-url-polyfill provides.
    expect(normalizeServerUrl("tenir.example.com")).toBe("wss://tenir.example.com/ws");
    expect(isValidServerUrl("tenir.example.com")).toBe(true);
  });
});

describe("mobile entrypoint", () => {
  it("installs the URL polyfill before loading the app", () => {
    const entry = readFileSync(resolve(process.cwd(), "index.js"), "utf8");
    const polyfillAt = entry.indexOf("react-native-url-polyfill/auto");
    const appAt = entry.indexOf("./src/App");
    expect(polyfillAt).toBeGreaterThanOrEqual(0);
    expect(appAt).toBeGreaterThanOrEqual(0);
    // The polyfill must be imported before the app so `URL` is patched first.
    expect(polyfillAt).toBeLessThan(appAt);
  });
});
