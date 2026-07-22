import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemStorage } from "./memStorage";

// Module state (client-core token store, even config) is reset per test via
// dynamic imports so silentLogin exercises the real login() path.
let creds: typeof import("../src/state/credentials");
let core: typeof import("@tenir/client-core");

beforeEach(async () => {
  vi.resetModules();
  creds = await import("../src/state/credentials");
  core = await import("@tenir/client-core");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("credentials store", () => {
  it("round-trips and clears cached credentials", async () => {
    const storage = new MemStorage();
    expect(await creds.loadCredentials(storage)).toBeNull();

    await creds.saveCredentials(storage, { username: "ada", password: "pw" });
    expect(await creds.loadCredentials(storage)).toEqual({ username: "ada", password: "pw" });

    await creds.clearCredentials(storage);
    expect(await creds.loadCredentials(storage)).toBeNull();
  });

  it("treats malformed or partial persisted JSON as absent", async () => {
    const storage = new MemStorage();
    storage.map.set(creds.CREDENTIALS_KEY, "not json");
    expect(await creds.loadCredentials(storage)).toBeNull();
    storage.map.set(creds.CREDENTIALS_KEY, JSON.stringify({ username: "ada" }));
    expect(await creds.loadCredentials(storage)).toBeNull();
  });
});

describe("silentLogin", () => {
  it("returns null without cached credentials (no network call)", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect(await creds.silentLogin(new MemStorage())).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("re-logs-in with the cached credentials and stores the fresh token", async () => {
    core.configureApi({ httpBaseUrl: "https://tenir.example.com" });
    const storage = new MemStorage();
    await creds.saveCredentials(storage, { username: "ada", password: "pw" });

    const principal = { userId: "u1", username: "ada", household: "h1", role: "member" };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).endsWith("/auth/login")) {
          return new Response(JSON.stringify({ token: "fresh-token" }), { status: 200 });
        }
        return new Response(JSON.stringify(principal), { status: 200 });
      }),
    );

    expect(await creds.silentLogin(storage)).toEqual(principal);
    expect(core.getToken()).toBe("fresh-token");
  });

  it("returns null when the server rejects the cached credentials", async () => {
    core.configureApi({ httpBaseUrl: "https://tenir.example.com" });
    const storage = new MemStorage();
    await creds.saveCredentials(storage, { username: "ada", password: "stale" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "bad credentials" }), { status: 401 })),
    );
    expect(await creds.silentLogin(storage)).toBeNull();
  });
});
