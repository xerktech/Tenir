import { describe, expect, it } from "vitest";

import { displayServerUrl, isValidServerUrl, normalizeServerUrl } from "../src/serverUrl";

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
  });
});

describe("displayServerUrl", () => {
  it("collapses the canonical secure form to the bare host people type", () => {
    expect(displayServerUrl("wss://tenir.example.com/ws")).toBe("tenir.example.com");
    expect(displayServerUrl("wss://tenir.example.com:9000/ws")).toBe("tenir.example.com:9000");
  });

  it("round-trips through normalizeServerUrl", () => {
    for (const canonical of ["wss://tenir.example.com/ws", "wss://h.example:9000/ws", "ws://localhost:8080/ws", "wss://h.example/custom"]) {
      expect(normalizeServerUrl(displayServerUrl(canonical))).toBe(canonical);
    }
  });

  it("keeps non-default forms (insecure scheme, custom path) in full", () => {
    expect(displayServerUrl("ws://localhost:8080/ws")).toBe("ws://localhost:8080/ws");
    expect(displayServerUrl("wss://h.example/custom")).toBe("wss://h.example/custom");
  });

  it("passes malformed input through untouched", () => {
    expect(displayServerUrl("garbage")).toBe("garbage");
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
