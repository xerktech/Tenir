/** Ported from upstream `packages/client-core/tests/serverUrl.test.ts` (vitest → bun:test). */

import { afterAll, beforeAll, describe, expect, it } from "bun:test";

import { displayServerUrl, isValidServerUrl, normalizeServerUrl } from "./serverUrl";

describe("normalizeServerUrl", () => {
  it("keeps a fully-qualified ws URL with a path", () => {
    expect(normalizeServerUrl("ws://localhost:8080/ws")).toBe("ws://localhost:8080/ws");
    expect(normalizeServerUrl("wss://example.com/ws")).toBe("wss://example.com/ws");
  });

  it("rewrites http(s) to ws(s)", () => {
    expect(normalizeServerUrl("http://localhost:8080/ws")).toBe("ws://localhost:8080/ws");
    expect(normalizeServerUrl("https://example.com/ws")).toBe("wss://example.com/ws");
  });

  it("defaults a bare host to wss:// and /ws", () => {
    expect(normalizeServerUrl("example.com")).toBe("wss://example.com/ws");
    expect(normalizeServerUrl("example.com:9000")).toBe("wss://example.com:9000/ws");
  });

  it("appends /ws only when no explicit path is given", () => {
    expect(normalizeServerUrl("wss://example.com")).toBe("wss://example.com/ws");
    expect(normalizeServerUrl("wss://example.com/")).toBe("wss://example.com/ws");
    expect(normalizeServerUrl("wss://example.com/custom")).toBe("wss://example.com/custom");
  });

  it("trims whitespace and drops query/hash", () => {
    expect(normalizeServerUrl("  wss://example.com/ws  ")).toBe("wss://example.com/ws");
    expect(normalizeServerUrl("wss://example.com/ws?x=1#y")).toBe("wss://example.com/ws");
  });

  it("returns empty for blank or unparseable input", () => {
    expect(normalizeServerUrl("")).toBe("");
    expect(normalizeServerUrl("   ")).toBe("");
    expect(normalizeServerUrl("wss://")).toBe("");
    expect(normalizeServerUrl("not a url")).toBe("");
    expect(normalizeServerUrl("example.com:notaport")).toBe("");
  });

  it("lowercases the scheme and host", () => {
    expect(normalizeServerUrl("WSS://Tenir.Example.COM")).toBe("wss://tenir.example.com/ws");
  });
});

// XERK-216 regression: the miniapp background JSContext (JSC / Zipline-QuickJS)
// has no `URL` global. The old implementation caught the resulting throw and
// returned "", so every login failed with "Enter your server address" no
// matter what the user typed. These suites re-run the parsers with `URL`
// removed to prove they no longer depend on it.
describe("without the URL global (miniapp background JSContext)", () => {
  let saved: PropertyDescriptor | undefined;

  beforeAll(() => {
    saved = Object.getOwnPropertyDescriptor(globalThis, "URL");
    delete (globalThis as Record<string, unknown>).URL;
  });

  afterAll(() => {
    if (saved) Object.defineProperty(globalThis, "URL", saved);
  });

  it("still normalizes a typed host", () => {
    expect(normalizeServerUrl("tenir.example.com")).toBe("wss://tenir.example.com/ws");
    expect(normalizeServerUrl("https://example.com")).toBe("wss://example.com/ws");
    expect(isValidServerUrl("example.com:9000")).toBe(true);
  });

  it("still rejects unparseable input", () => {
    expect(normalizeServerUrl("wss://")).toBe("");
    expect(normalizeServerUrl("not a url")).toBe("");
  });

  it("still renders the display form", () => {
    expect(displayServerUrl("wss://tenir.example.com/ws")).toBe("tenir.example.com");
    expect(displayServerUrl("ws://localhost:8080/ws")).toBe("localhost:8080");
  });
});

describe("isValidServerUrl", () => {
  it("accepts hosts in any accepted form", () => {
    expect(isValidServerUrl("example.com")).toBe(true);
    expect(isValidServerUrl("ws://localhost:8080/ws")).toBe(true);
    expect(isValidServerUrl("https://example.com")).toBe(true);
  });

  it("rejects blank or schemeless-hostless input", () => {
    expect(isValidServerUrl("")).toBe(false);
    expect(isValidServerUrl("   ")).toBe(false);
    expect(isValidServerUrl("wss://")).toBe(false);
  });
});

describe("displayServerUrl", () => {
  it("strips the ws(s):// scheme and default /ws path", () => {
    expect(displayServerUrl("wss://tenir.example.com/ws")).toBe("tenir.example.com");
    expect(displayServerUrl("ws://localhost:8080/ws")).toBe("localhost:8080");
  });

  it("preserves a non-default port and path so it round-trips", () => {
    expect(displayServerUrl("wss://example.com:9000/ws")).toBe("example.com:9000");
    expect(displayServerUrl("wss://example.com/api/ws")).toBe("example.com/api/ws");
    // Round-trip: display form re-normalizes back to the canonical URL.
    expect(normalizeServerUrl(displayServerUrl("wss://example.com/api/ws"))).toBe(
      "wss://example.com/api/ws",
    );
  });

  it("drops a bare or trailing-slash path", () => {
    expect(displayServerUrl("wss://example.com")).toBe("example.com");
    expect(displayServerUrl("wss://example.com/")).toBe("example.com");
  });

  it("trims whitespace and returns a bare host unchanged", () => {
    expect(displayServerUrl("  wss://example.com/ws  ")).toBe("example.com");
    expect(displayServerUrl("tenir.example.com")).toBe("tenir.example.com");
  });

  it("returns empty for blank input", () => {
    expect(displayServerUrl("")).toBe("");
    expect(displayServerUrl("   ")).toBe("");
  });
});
