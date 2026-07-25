import { describe, expect, it } from "vitest";

import {
  clearSessionId,
  createMirroredTokenStore,
  loadLastTab,
  loadServerUrl,
  loadSessionId,
  loadThemeMode,
  memoryKeyValue,
  saveLastTab,
  saveServerUrl,
  saveSessionId,
  saveThemeMode,
} from "../src/storage";

describe("memoryKeyValue", () => {
  it("round-trips values and seeds from an initial map", async () => {
    const kv = memoryKeyValue({ seeded: "1" });
    expect(await kv.getItem("seeded")).toBe("1");
    expect(await kv.getItem("missing")).toBeNull();
    await kv.setItem("k", "v");
    expect(await kv.getItem("k")).toBe("v");
    await kv.removeItem("k");
    expect(await kv.getItem("k")).toBeNull();
  });
});

describe("createMirroredTokenStore", () => {
  it("hydrates the in-memory mirror from the backing store", async () => {
    const kv = memoryKeyValue({ "tenir.token": "persisted.jwt" });
    const m = createMirroredTokenStore(kv);
    expect(m.store.get()).toBeNull(); // not yet hydrated
    await m.hydrate();
    expect(m.store.get()).toBe("persisted.jwt"); // synchronous read after hydrate
  });

  it("reads synchronously while persisting writes in the background", async () => {
    const kv = memoryKeyValue();
    const m = createMirroredTokenStore(kv);

    m.store.set("new.jwt");
    expect(m.store.get()).toBe("new.jwt"); // immediately visible to synchronous callers
    await Promise.resolve(); // let the background write settle
    expect(await kv.getItem("tenir.token")).toBe("new.jwt");

    m.store.clear();
    expect(m.store.get()).toBeNull();
    await Promise.resolve();
    expect(await kv.getItem("tenir.token")).toBeNull();
  });

  it("reports background persistence failures via onError", async () => {
    const failing = {
      getItem: () => Promise.reject(new Error("boom")),
      setItem: () => Promise.reject(new Error("boom")),
      removeItem: () => Promise.resolve(),
    };
    const errors: unknown[] = [];
    const m = createMirroredTokenStore(failing, (e) => errors.push(e));
    await m.hydrate();
    expect(m.store.get()).toBeNull(); // hydrate failure falls back to no token
    m.store.set("x");
    await Promise.resolve();
    expect(errors.length).toBeGreaterThanOrEqual(1);
  });
});

describe("server URL persistence", () => {
  it("saves and loads the chosen api URL", async () => {
    const kv = memoryKeyValue();
    expect(await loadServerUrl(kv)).toBeNull();
    await saveServerUrl(kv, "wss://home.example/ws");
    expect(await loadServerUrl(kv)).toBe("wss://home.example/ws");
  });
});

describe("theme mode persistence", () => {
  it("saves and loads the chosen theme mode", async () => {
    const kv = memoryKeyValue();
    expect(await loadThemeMode(kv)).toBeNull();
    await saveThemeMode(kv, "light");
    expect(await loadThemeMode(kv)).toBe("light");
  });

  it("uses the same key as the web SPA and rejects junk values", async () => {
    // One `tenir.theme` mental model across clients (web/src/theme.ts).
    const kv = memoryKeyValue({ "tenir.theme": "dark" });
    expect(await loadThemeMode(kv)).toBe("dark");
    await kv.setItem("tenir.theme", "solarized");
    expect(await loadThemeMode(kv)).toBeNull();
  });
});

describe("last tab persistence", () => {
  it("saves and loads the last dashboard tab (XERK-80 relaunch parity)", async () => {
    const kv = memoryKeyValue();
    expect(await loadLastTab(kv)).toBeNull(); // first launch: no saved tab
    await saveLastTab(kv, "History");
    expect(await loadLastTab(kv)).toBe("History");
  });

  it("swallows storage failures (best-effort persistence)", async () => {
    const failing = {
      getItem: () => Promise.reject(new Error("boom")),
      setItem: () => Promise.reject(new Error("boom")),
      removeItem: () => Promise.resolve(),
    };
    await expect(saveLastTab(failing, "Status")).resolves.toBeUndefined();
    await expect(loadLastTab(failing)).resolves.toBeNull();
  });
});

describe("capture session id persistence", () => {
  it("saves, loads, and clears the per-device session id", async () => {
    const kv = memoryKeyValue();
    expect(await loadSessionId(kv)).toBeNull();
    await saveSessionId(kv, "sess-123");
    expect(await loadSessionId(kv)).toBe("sess-123");
    await clearSessionId(kv);
    expect(await loadSessionId(kv)).toBeNull();
  });
});
