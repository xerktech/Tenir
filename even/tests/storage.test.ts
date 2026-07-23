import { beforeEach, describe, expect, it, vi } from "vitest";

import { BridgeStorage, BrowserStorage, withBleTimeout, type StorageBridge } from "../src/state/storage";

describe("BrowserStorage", () => {
  beforeEach(() => window.localStorage.clear());

  it("round-trips and removes values via window.localStorage", async () => {
    const store = new BrowserStorage();
    expect(await store.get("k")).toBeNull();

    await store.set("k", "v");
    expect(await store.get("k")).toBe("v");
    expect(window.localStorage.getItem("k")).toBe("v");

    await store.remove("k");
    expect(await store.get("k")).toBeNull();
  });
});

describe("BridgeStorage", () => {
  function fakeBridge(overrides: Partial<StorageBridge> = {}): StorageBridge & { store: Map<string, string> } {
    const store = new Map<string, string>();
    return {
      store,
      // The SDK resolves "" for a missing key rather than rejecting.
      getLocalStorage: async (key: string) => store.get(key) ?? "",
      setLocalStorage: async (key: string, value: string) => {
        store.set(key, value);
        return true;
      },
      ...overrides,
    };
  }

  it("round-trips values through the bridge", async () => {
    const bridge = fakeBridge();
    const storage = new BridgeStorage(bridge);
    await storage.set("k", "v");
    expect(bridge.store.get("k")).toBe("v");
    expect(await storage.get("k")).toBe("v");
  });

  it("treats the SDK's empty-string miss as null", async () => {
    const storage = new BridgeStorage(fakeBridge());
    expect(await storage.get("absent")).toBeNull();
  });

  it("remove() writes an empty value, which reads back as a miss", async () => {
    const bridge = fakeBridge();
    const storage = new BridgeStorage(bridge);
    await storage.set("k", "v");
    await storage.remove("k");
    expect(bridge.store.get("k")).toBe(""); // the SDK has no delete
    expect(await storage.get("k")).toBeNull();
  });

  it("bounds a hung BLE call: withBleTimeout resolves the fallback (XERK-82)", async () => {
    // withBleTimeout is what BridgeStorage/persist race every bridge call
    // through, so a flaky ~30s BLE hop can't stall the boot path.
    const never = new Promise<string>(() => {});
    expect(await withBleTimeout(never, "fallback", 20)).toBe("fallback");
    // A call that answers in time wins the race untouched.
    expect(await withBleTimeout(Promise.resolve("value"), "fallback", 20)).toBe("value");
  });

  it("swallows bridge failures (a dropped write/read is best-effort)", async () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const storage = new BridgeStorage({
        getLocalStorage: async () => {
          throw new Error("BLE down");
        },
        setLocalStorage: async () => {
          throw new Error("BLE down");
        },
      });
      expect(await storage.get("k")).toBeNull();
      await expect(storage.set("k", "v")).resolves.toBeUndefined();
      await expect(storage.remove("k")).resolves.toBeUndefined();
    } finally {
      err.mockRestore();
    }
  });
});
