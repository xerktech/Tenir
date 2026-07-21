import { describe, expect, it } from "vitest";

import { apiBaseUrl, configureApi, httpBaseFromWs, wsFromHttpBase } from "../src/config";

describe("api config", () => {
  it("defaults to localhost and accepts an override (stripping trailing slash)", () => {
    expect(apiBaseUrl()).toBe("http://localhost:8080");
    configureApi({ httpBaseUrl: "https://gw.example.com/" });
    expect(apiBaseUrl()).toBe("https://gw.example.com");
    configureApi({ httpBaseUrl: "http://localhost:8080" });
  });

  it("derives the REST base from a WS URL", () => {
    expect(httpBaseFromWs("ws://localhost:8080/ws")).toBe("http://localhost:8080");
    expect(httpBaseFromWs("wss://gw.example.com/ws")).toBe("https://gw.example.com");
    expect(httpBaseFromWs("wss://gw.example.com:9000/ws?x=1")).toBe("https://gw.example.com:9000");
  });

  it("falls back to the default on an unparseable WS URL", () => {
    expect(httpBaseFromWs("not a url")).toBe("http://localhost:8080");
  });
});

describe("wsFromHttpBase", () => {
  it("derives the WS URL from a REST base", () => {
    expect(wsFromHttpBase("http://localhost:8080")).toBe("ws://localhost:8080/ws");
    expect(wsFromHttpBase("https://gw.example.com")).toBe("wss://gw.example.com/ws");
    expect(wsFromHttpBase("https://gw.example.com:9000/")).toBe("wss://gw.example.com:9000/ws");
  });

  it("falls back to the default on an unparseable base", () => {
    expect(wsFromHttpBase("not a url")).toBe("ws://localhost:8080/ws");
  });
});

// When the api is reverse-proxied behind the web origin under `/api` (single-URL
// deployment — see web/nginx.conf), the base carries a path prefix. REST and WS
// URLs must both keep that prefix so requests land on the proxy, not the root.
describe("subpath (/api) deployment", () => {
  it("preserves the /api prefix when deriving the WS URL", () => {
    expect(wsFromHttpBase("https://tenir.example.com/api")).toBe(
      "wss://tenir.example.com/api/ws",
    );
    expect(wsFromHttpBase("http://localhost:5174/api")).toBe(
      "ws://localhost:5174/api/ws",
    );
  });

  it("round-trips the /api base back from its WS URL", () => {
    expect(httpBaseFromWs("wss://tenir.example.com/api/ws")).toBe(
      "https://tenir.example.com/api",
    );
  });
});
