import { beforeEach, describe, expect, it } from "vitest";

import {
  CUE_LEVEL_KEY,
  FALLBACK_WS_URL,
  loadCueLevel,
  localStorageServerUrlStore,
  normalizeWsUrl,
  resolveWsUrl,
  saveCueLevel,
  SERVER_URL_KEY,
} from "../src/state/settings";

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

describe("localStorageServerUrlStore", () => {
  beforeEach(() => localStorage.clear());

  it("round-trips and clears the saved URL", () => {
    const store = localStorageServerUrlStore();
    expect(store.load()).toBeNull();

    store.save("wss://home.example/ws");
    expect(store.load()).toBe("wss://home.example/ws");
    expect(localStorage.getItem(SERVER_URL_KEY)).toBe("wss://home.example/ws");

    store.clear();
    expect(store.load()).toBeNull();
  });
});

describe("cue level", () => {
  beforeEach(() => localStorage.clear());

  it("defaults to balanced and round-trips a saved level", () => {
    expect(loadCueLevel()).toBe("balanced");
    saveCueLevel("aggressive");
    expect(loadCueLevel()).toBe("aggressive");
    expect(localStorage.getItem(CUE_LEVEL_KEY)).toBe("aggressive");
  });

  it("falls back to the default for an unrecognized stored value", () => {
    localStorage.setItem(CUE_LEVEL_KEY, "bogus");
    expect(loadCueLevel()).toBe("balanced");
  });
});
