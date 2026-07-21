import { describe, expect, it } from "vitest";

import { isValidServerUrl, normalizeServerUrl } from "../src/lib/serverUrl";

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

  it("accepts a plain domain typed into the setup field (XERK-57)", () => {
    // The Android/mobile setup screen invites a plain address like `tenir.example.com`
    // rather than a full `wss://…/ws` URL; it must expand to the canonical form.
    expect(normalizeServerUrl("Tenir.example.com")).toBe("wss://tenir.example.com/ws");
    expect(isValidServerUrl("Tenir.example.com")).toBe(true);
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
