import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SERVER_URL_KEY } from "../src/state/settings";
import { MemStorage } from "./memStorage";

// config.ts and client-core both carry module state (current URLs, the token
// store); reset per test so initConfig runs fresh each time.
let cfg: typeof import("../src/config");
let core: typeof import("@tenir/client-core");

beforeEach(async () => {
  vi.resetModules();
  cfg = await import("../src/config");
  core = await import("@tenir/client-core");
});

// Flush the token store's fire-and-forget persistence writes.
const settle = () => new Promise((r) => setTimeout(r, 0));

afterEach(() => {
  // The OIDC sidecar store write-throughs to localStorage; clear it so state
  // doesn't leak between tests.
  try {
    localStorage.clear();
  } catch {
    /* no localStorage in this env */
  }
});

describe("deviceTokenStore", () => {
  it("mirrors the token in memory and write-throughs to the device store", async () => {
    const storage = new MemStorage();
    const store = cfg.deviceTokenStore(storage, null);
    expect(store.get()).toBeNull();

    store.set("tok");
    expect(store.get()).toBe("tok"); // synchronous for client-core
    await settle();
    expect(storage.map.get(cfg.TOKEN_KEY)).toBe("tok");

    store.clear();
    expect(store.get()).toBeNull();
    await settle();
    expect(storage.map.has(cfg.TOKEN_KEY)).toBe(false);
  });

  it("starts from the persisted token", () => {
    const store = cfg.deviceTokenStore(new MemStorage(), "cached");
    expect(store.get()).toBe("cached");
  });
});

describe("initConfig", () => {
  it("loads the persisted server URL and token from the device store", async () => {
    const storage = new MemStorage();
    storage.map.set(SERVER_URL_KEY, "wss://home.example/ws");
    storage.map.set(cfg.TOKEN_KEY, "cached-token");

    await cfg.initConfig(storage);

    expect(cfg.isServerConfigured()).toBe(true);
    expect(cfg.config.apiWsUrl).toBe("wss://home.example/ws");
    expect(cfg.config.apiHttpUrl).toBe("https://home.example");
    expect(core.apiBaseUrl()).toBe("https://home.example");
    // The cached sign-in is live without any user input (XERK-82).
    expect(core.getToken()).toBe("cached-token");
  });

  it("falls back to the seed/localhost when nothing is persisted", async () => {
    await cfg.initConfig(new MemStorage());
    expect(cfg.isServerConfigured()).toBe(false);
    expect(cfg.config.apiWsUrl).toBe("ws://localhost:8080/ws");
    expect(core.getToken()).toBeNull();
  });
});

describe("OIDC sidecar (XERK-656)", () => {
  it("re-seeds a persisted PKCE transaction so the authorize redirect survives a restart", async () => {
    // The Even WebView returning from Authentik may be restart-like: localStorage
    // wiped, but the transaction was write-through'd to the device store before
    // the redirect. initConfig must recover it so the code exchange can complete.
    localStorage.clear();
    const storage = new MemStorage();
    storage.map.set(cfg.OIDC_TX_KEY, JSON.stringify({ verifier: "v", state: "st", nonce: "no" }));

    await cfg.initConfig(storage);

    expect(core.getOidcTransaction()).toEqual({ verifier: "v", state: "st", nonce: "no" });
  });

  it("re-seeds a persisted OIDC session so a signed-in OIDC user survives a restart", async () => {
    localStorage.clear();
    const storage = new MemStorage();
    storage.map.set(cfg.OIDC_SESSION_KEY, JSON.stringify({ refreshToken: "r", expiresAt: 123 }));

    await cfg.initConfig(storage);

    expect(core.getSessionKind()).toBe("oidc");
    expect(core.getOidcSession()).toEqual({ refreshToken: "r", expiresAt: 123 });
  });

  it("stays built-in with nothing persisted", async () => {
    localStorage.clear();
    await cfg.initConfig(new MemStorage());
    expect(core.getSessionKind()).toBe("builtin");
    expect(core.getOidcTransaction()).toBeNull();
  });
});

describe("applyServerUrl", () => {
  it("persists the choice and repoints the REST client", async () => {
    const storage = new MemStorage();
    await cfg.initConfig(storage);

    const applied = await cfg.applyServerUrl("wss://tenir.example.com/ws");
    expect(applied).toEqual({
      wsUrl: "wss://tenir.example.com/ws",
      httpBaseUrl: "https://tenir.example.com",
    });
    expect(storage.map.get(SERVER_URL_KEY)).toBe("wss://tenir.example.com/ws");
    expect(cfg.isServerConfigured()).toBe(true);
    expect(core.apiBaseUrl()).toBe("https://tenir.example.com");
  });

  it("rejects a non-ws(s) URL without persisting it", async () => {
    const storage = new MemStorage();
    await cfg.initConfig(storage);
    expect(await cfg.applyServerUrl("https://tenir.example.com")).toBeNull();
    expect(storage.map.has(SERVER_URL_KEY)).toBe(false);
  });
});
