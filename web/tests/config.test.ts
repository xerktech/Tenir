import { afterEach, describe, expect, it, vi } from "vitest";

// Stub the client-core symbols web/src/config.ts touches so importing the module
// (which configures the REST client as a side effect) is inert.
const { configureApi, setToken } = vi.hoisted(() => ({ configureApi: vi.fn(), setToken: vi.fn() }));
vi.mock("@tenir/client-core", () => ({ configureApi, setToken }));

afterEach(() => {
  vi.resetModules();
  vi.unstubAllEnvs();
  configureApi.mockClear();
  setToken.mockClear();
});

describe("web api config", () => {
  it("prefers the VITE_API_HTTP dev seed, trimming a trailing slash", async () => {
    vi.stubEnv("VITE_API_HTTP", "http://dev-api:8080/");
    const { getServerUrl } = await import("../src/config");
    expect(getServerUrl()).toBe("http://dev-api:8080");
    // The shared REST client is pointed at the resolved URL on import.
    expect(configureApi).toHaveBeenCalledWith({ httpBaseUrl: "http://dev-api:8080" });
  });

  it("falls back to the page's own origin (the api serves the SPA)", async () => {
    // No dev seed (a blank value is treated as unset): production serves the SPA
    // from the api container, so the api is simply this page's origin. jsdom runs
    // this suite at https://localhost/ (vite.config.ts environmentOptions).
    vi.stubEnv("VITE_API_HTTP", "");
    const { getServerUrl } = await import("../src/config");
    expect(getServerUrl()).toBe("https://localhost");
    expect(configureApi).toHaveBeenCalledWith({ httpBaseUrl: "https://localhost" });
  });

  it("no longer reads the removed window.__TENIR_SERVER_URL__ injection", async () => {
    vi.stubEnv("VITE_API_HTTP", "");
    (window as unknown as Record<string, unknown>).__TENIR_SERVER_URL__ = "https://stale.example.com";
    try {
      const { getServerUrl } = await import("../src/config");
      expect(getServerUrl()).toBe("https://localhost");
    } finally {
      delete (window as unknown as Record<string, unknown>).__TENIR_SERVER_URL__;
    }
  });
});

describe("adoptTokenFromUrl (XERK-82: Even G2 phone page hand-over)", () => {
  // A minimal window stand-in: jsdom's real location.hash is awkward to mutate
  // per-test, and the function only touches location + history.replaceState.
  const fakeWin = (hash: string) => {
    const history = { replaceState: vi.fn() };
    return {
      win: {
        location: { hash, pathname: "/", search: "" } as unknown as Location,
        history: history as unknown as History,
      },
      history,
    };
  };

  it("adopts the token from the #token= fragment and strips it from the URL", async () => {
    const { adoptTokenFromUrl } = await import("../src/config");
    const { win, history } = fakeWin("#token=abc%2F123");
    adoptTokenFromUrl(win);
    expect(setToken).toHaveBeenCalledWith("abc/123");
    // The fragment (with the token) is removed from the address bar/history.
    expect(history.replaceState).toHaveBeenCalledWith(null, "", "/");
  });

  it("does nothing when the fragment carries no token", async () => {
    const { adoptTokenFromUrl } = await import("../src/config");
    const { win, history } = fakeWin("#other=1");
    adoptTokenFromUrl(win);
    expect(setToken).not.toHaveBeenCalled();
    expect(history.replaceState).not.toHaveBeenCalled();
  });

  it("is adopted on module import (before the app's first me())", async () => {
    vi.stubEnv("VITE_API_HTTP", "");
    window.location.hash = "#token=boot-token";
    try {
      await import("../src/config");
      expect(setToken).toHaveBeenCalledWith("boot-token");
    } finally {
      window.location.hash = "";
    }
  });
});
