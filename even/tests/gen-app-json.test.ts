import { describe, expect, it } from "vitest";

// @ts-expect-error — plain .mjs build script, no types
import { buildWhitelist } from "../scripts/gen-app-json.mjs";

describe("buildWhitelist", () => {
  it("falls back to localhost when unset/empty so dev pack keeps working", () => {
    expect(buildWhitelist(undefined)).toEqual(["https://localhost", "wss://localhost"]);
    expect(buildWhitelist("")).toEqual(["https://localhost", "wss://localhost"]);
  });

  it("wraps a single host as https + wss origins", () => {
    expect(buildWhitelist("api.example.com")).toEqual([
      "https://api.example.com",
      "wss://api.example.com",
    ]);
  });

  it("expands comma-separated hosts, trimming whitespace", () => {
    expect(buildWhitelist("a.com, b.com")).toEqual([
      "https://a.com",
      "wss://a.com",
      "https://b.com",
      "wss://b.com",
    ]);
  });

  it("passes a bare '*' through as a wildcard origin (BYO any host)", () => {
    expect(buildWhitelist("*")).toEqual(["https://*", "wss://*"]);
  });

  it("supports a subdomain wildcard", () => {
    expect(buildWhitelist("*.example.com")).toEqual([
      "https://*.example.com",
      "wss://*.example.com",
    ]);
  });

  it("combines a wildcard with explicit hosts", () => {
    expect(buildWhitelist("*, localhost")).toEqual([
      "https://*",
      "wss://*",
      "https://localhost",
      "wss://localhost",
    ]);
  });
});
