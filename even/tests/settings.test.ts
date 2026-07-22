import { describe, expect, it } from "vitest";

import {
  FALLBACK_WS_URL,
  loadServerUrl,
  normalizeWsUrl,
  resolveWsUrl,
  saveServerUrl,
  SERVER_URL_KEY,
} from "../src/state/settings";
import { MemStorage } from "./memStorage";

describe("normalizeWsUrl", () => {
  it("accepts ws:// and wss:// URLs, trimming whitespace and a trailing slash", () => {
    expect(normalizeWsUrl("  wss://home.example/ws  ")).toBe("wss://home.example/ws");
    expect(normalizeWsUrl("ws://localhost:8080/ws/")).toBe("ws://localhost:8080/ws");
  });

  it("rejects empty, non-ws(s), and malformed URLs", () => {
    expect(normalizeWsUrl("")).toBeNull();
    expect(normalizeWsUrl("   ")).toBeNull();
    expect(normalizeWsUrl("https://home.example/ws")).toBeNull();
    expect(normalizeWsUrl("not a url")).toBeNull();
  });
});

describe("resolveWsUrl", () => {
  it("prefers a saved choice over the build seed", () => {
    expect(resolveWsUrl("wss://saved/ws", "ws://seed/ws")).toBe("wss://saved/ws");
  });

  it("falls back to the seed when nothing is saved", () => {
    expect(resolveWsUrl(null, "ws://seed/ws")).toBe("ws://seed/ws");
  });

  it("skips an invalid saved value and uses the seed", () => {
    expect(resolveWsUrl("garbage", "ws://seed/ws")).toBe("ws://seed/ws");
  });

  it("falls back to localhost when neither is valid", () => {
    expect(resolveWsUrl(null, null)).toBe(FALLBACK_WS_URL);
    expect(resolveWsUrl("nope", "also-nope")).toBe(FALLBACK_WS_URL);
  });
});

describe("loadServerUrl / saveServerUrl", () => {
  it("round-trips the saved URL through the device store", async () => {
    const storage = new MemStorage();
    expect(await loadServerUrl(storage)).toBeNull();

    await saveServerUrl(storage, "wss://home.example/ws");
    expect(await loadServerUrl(storage)).toBe("wss://home.example/ws");
    expect(storage.map.get(SERVER_URL_KEY)).toBe("wss://home.example/ws");
  });

  it("treats a malformed persisted value as unconfigured", async () => {
    const storage = new MemStorage();
    storage.map.set(SERVER_URL_KEY, "garbage");
    expect(await loadServerUrl(storage)).toBeNull();
  });
});
