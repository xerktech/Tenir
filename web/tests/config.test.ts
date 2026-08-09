import { afterEach, describe, expect, it, vi } from "vitest";

// Stub the client-core symbols web/src/config.ts touches so importing the module
// (which configures the REST client as a side effect) is inert.
const { configureApi, setToken, getToken } = vi.hoisted(() => ({
  configureApi: vi.fn(),
  setToken: vi.fn(),
  // Signed out by default; the takeover tests below stub a stored token.
  getToken: vi.fn(() => null as string | null),
}));
vi.mock("@tenir/client-core", () => ({ configureApi, setToken, getToken }));

afterEach(() => {
  vi.resetModules();
  vi.unstubAllEnvs();
  configureApi.mockClear();
  setToken.mockClear();
  getToken.mockReset();
  getToken.mockReturnValue(null);
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

  it("refuses to replace a session that is already signed in (XERK-236)", async () => {
    // Any link — a chat message, an <img src>, a redirect — carrying #token=
    // used to silently swap the signed-in account: the victim's history
    // vanished, everything they recorded afterwards landed in the attacker's
    // household, and the fragment was scrubbed so nothing looked wrong.
    getToken.mockReturnValue("the-victims-own-token");
    const { adoptTokenFromUrl } = await import("../src/config");
    const { win, history } = fakeWin("#token=attacker-token");
    adoptTokenFromUrl(win);
    expect(setToken).not.toHaveBeenCalled();
    // The token is still scrubbed from the address bar rather than left on show.
    expect(history.replaceState).toHaveBeenCalledWith(null, "", "/");
  });

  it("#token=garbage cannot destroy a working session", async () => {
    getToken.mockReturnValue("the-victims-own-token");
    const { adoptTokenFromUrl } = await import("../src/config");
    const { win } = fakeWin("#token=not-a-real-token");
    adoptTokenFromUrl(win);
    expect(setToken).not.toHaveBeenCalled();
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
