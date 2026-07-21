import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  authHeader,
  clearToken,
  configureTokenStore,
  getToken,
  setToken,
  withToken,
  type TokenStore,
} from "../src/auth";

describe("auth token storage", () => {
  beforeEach(() => clearToken());

  it("round-trips a token through storage", () => {
    expect(getToken()).toBeNull();
    setToken("abc.def");
    expect(getToken()).toBe("abc.def");
    clearToken();
    expect(getToken()).toBeNull();
  });

  it("builds an Authorization header only when a token is present", () => {
    expect(authHeader()).toEqual({});
    setToken("t0ken");
    expect(authHeader()).toEqual({ Authorization: "Bearer t0ken" });
  });

  it("appends ?token= to a WS URL, picking the right separator", () => {
    expect(withToken("ws://h/ws")).toBe("ws://h/ws");
    setToken("a b"); // forces url-encoding
    expect(withToken("ws://h/ws")).toBe("ws://h/ws?token=a%20b");
    expect(withToken("ws://h/ws?x=1")).toBe("ws://h/ws?x=1&token=a%20b");
  });
});

describe("configureTokenStore", () => {
  // Restore the default localStorage-backed store so other suites are unaffected.
  afterEach(() => {
    configureTokenStore({
      get: () => {
        try {
          return localStorage.getItem("tenir.token");
        } catch {
          return null;
        }
      },
      set: (t) => localStorage.setItem("tenir.token", t),
      clear: () => localStorage.removeItem("tenir.token"),
    });
    clearToken();
  });

  it("routes token reads/writes through an injected store (the mobile keychain seam)", () => {
    let backing: string | null = null;
    const memory: TokenStore = {
      get: () => backing,
      set: (t) => {
        backing = t;
      },
      clear: () => {
        backing = null;
      },
    };
    configureTokenStore(memory);

    expect(getToken()).toBeNull();
    setToken("native.jwt");
    expect(backing).toBe("native.jwt"); // written to the injected store, not localStorage
    expect(authHeader()).toEqual({ Authorization: "Bearer native.jwt" });
    clearToken();
    expect(backing).toBeNull();
  });
});
