import { describe, expect, it } from "bun:test";

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

describe("configureApi", () => {
  it("strips a trailing slash and drives apiBaseUrl", () => {
    configureApi({ httpBaseUrl: "https://example.com/" });
    expect(apiBaseUrl()).toBe("https://example.com");
  });
});
