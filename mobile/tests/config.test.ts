import { describe, expect, it } from "vitest";

import { apiBaseUrl } from "@tenir/client-core";

import { configureApiFromWs } from "../src/config";

describe("configureApiFromWs", () => {
  it("derives the REST base from a wss:// api URL and configures client-core", () => {
    const { httpBaseUrl } = configureApiFromWs("wss://example.com:8080/ws");
    expect(httpBaseUrl).toBe("https://example.com:8080");
    // The shared client-core REST base is pointed at the same instance.
    expect(apiBaseUrl()).toBe("https://example.com:8080");
  });

  it("maps ws:// to http:// for a local dev api", () => {
    const { httpBaseUrl } = configureApiFromWs("ws://localhost:8080/ws");
    expect(httpBaseUrl).toBe("http://localhost:8080");
  });
});
