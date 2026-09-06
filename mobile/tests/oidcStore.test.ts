import { describe, expect, it } from "vitest";

import { createMirroredOidcStore } from "../src/lib/oidcStore";
import { memoryKeyValue, type KeyValueStore } from "../src/storage";

describe("createMirroredOidcStore", () => {
  it("reads back synchronously what it writes", () => {
    const { store } = createMirroredOidcStore(memoryKeyValue());
    expect(store.get("tenir.oidc.session")).toBeNull();
    store.set("tenir.oidc.session", "{\"refreshToken\":\"r\"}");
    expect(store.get("tenir.oidc.session")).toBe("{\"refreshToken\":\"r\"}");
    store.remove("tenir.oidc.session");
    expect(store.get("tenir.oidc.session")).toBeNull();
  });

  it("persists writes through to the async backing store", async () => {
    const kv = memoryKeyValue();
    const { store } = createMirroredOidcStore(kv);
    store.set("tenir.oidc.tx", "txdata");
    // Written through in the background — visible on the underlying kv.
    await Promise.resolve();
    expect(await kv.getItem("tenir.oidc.tx")).toBe("txdata");

    store.remove("tenir.oidc.tx");
    await Promise.resolve();
    expect(await kv.getItem("tenir.oidc.tx")).toBeNull();
  });

  it("hydrates OIDC keys from the backing store, ignoring unrelated keys", async () => {
    const kv = memoryKeyValue({
      "tenir.oidc.session": "session-json",
      "tenir.oidc.tx": "tx-json",
      "tenir.token": "bearer", // owned by the token mirror, not the OIDC sidecar
      "tenir.theme": "dark",
    });
    const mirrored = createMirroredOidcStore(kv);
    // Nothing in the mirror until hydrate runs.
    expect(mirrored.store.get("tenir.oidc.session")).toBeNull();

    await mirrored.hydrate();
    expect(mirrored.store.get("tenir.oidc.session")).toBe("session-json");
    expect(mirrored.store.get("tenir.oidc.tx")).toBe("tx-json");
    // The bearer token is outside the OIDC prefix — not pulled into this mirror.
    expect(mirrored.store.get("tenir.token")).toBeNull();
  });

  it("degrades to no-op hydrate when the store cannot enumerate keys", async () => {
    const base = memoryKeyValue({ "tenir.oidc.session": "s" });
    const noEnumerate: KeyValueStore = {
      getItem: base.getItem,
      setItem: base.setItem,
      removeItem: base.removeItem,
      // getAllKeys deliberately omitted
    };
    const mirrored = createMirroredOidcStore(noEnumerate);
    await expect(mirrored.hydrate()).resolves.toBeUndefined();
    expect(mirrored.store.get("tenir.oidc.session")).toBeNull();
  });

  it("reports background persistence failures via onError", async () => {
    let captured: unknown;
    const failing: KeyValueStore = {
      getItem: () => Promise.resolve(null),
      setItem: () => Promise.reject(new Error("disk full")),
      removeItem: () => Promise.resolve(),
      getAllKeys: () => Promise.resolve([]),
    };
    const { store } = createMirroredOidcStore(failing, (e) => {
      captured = e;
    });
    store.set("tenir.oidc.session", "x");
    // Even when persistence fails, the in-memory mirror still holds the value.
    expect(store.get("tenir.oidc.session")).toBe("x");
    await Promise.resolve();
    await Promise.resolve();
    expect((captured as Error).message).toBe("disk full");
  });
});
