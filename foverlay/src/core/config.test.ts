import { afterAll, beforeAll, describe, expect, it } from "bun:test";

import { apiBaseUrl, configureApi, httpBaseFromWs, wsFromHttpBase } from "./config";

describe("httpBaseFromWs", () => {
  it("maps wss to https and drops the /ws path", () => {
    expect(httpBaseFromWs("wss://tenir.example.com/ws")).toBe("https://tenir.example.com");
  });

  it("maps ws to http and keeps port + extra path", () => {
    expect(httpBaseFromWs("ws://localhost:8080/ws")).toBe("http://localhost:8080");
    expect(httpBaseFromWs("wss://example.com/api/ws")).toBe("https://example.com/api");
  });

  it("falls back to the localhost default on garbage", () => {
    expect(httpBaseFromWs("not a url")).toBe("http://localhost:8080");
  });
});

describe("wsFromHttpBase", () => {
  it("round-trips with httpBaseFromWs", () => {
    expect(wsFromHttpBase("https://tenir.example.com")).toBe("wss://tenir.example.com/ws");
    expect(wsFromHttpBase(httpBaseFromWs("ws://localhost:8080/ws"))).toBe(
      "ws://localhost:8080/ws",
    );
  });
});

// XERK-216 regression: these derivations run in the miniapp background
// JSContext, which has no `URL` global — the old `new URL` + catch fallback
// silently misrouted every request to localhost there.
describe("without the URL global (miniapp background JSContext)", () => {
  let saved: PropertyDescriptor | undefined;

  beforeAll(() => {
    saved = Object.getOwnPropertyDescriptor(globalThis, "URL");
    delete (globalThis as Record<string, unknown>).URL;
  });

  afterAll(() => {
    if (saved) Object.defineProperty(globalThis, "URL", saved);
  });

  it("still derives the REST base from a WS URL", () => {
    expect(httpBaseFromWs("wss://tenir.example.com/ws")).toBe("https://tenir.example.com");
    expect(httpBaseFromWs("ws://localhost:8080/ws")).toBe("http://localhost:8080");
  });

  it("still derives the WS URL from a REST base", () => {
    expect(wsFromHttpBase("https://tenir.example.com")).toBe("wss://tenir.example.com/ws");
  });
});

describe("configureApi", () => {
  it("strips a trailing slash and drives apiBaseUrl", () => {
    configureApi({ httpBaseUrl: "https://example.com/" });
    expect(apiBaseUrl()).toBe("https://example.com");
  });
});
