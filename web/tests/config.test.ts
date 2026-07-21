import { afterEach, describe, expect, it, vi } from "vitest";

// configureApi is the only client-core symbol web/src/config.ts touches; stub it so
// importing the module (which configures the REST client as a side effect) is inert.
const { configureApi } = vi.hoisted(() => ({ configureApi: vi.fn() }));
vi.mock("@tenir/client-core", () => ({ configureApi }));

afterEach(() => {
  vi.resetModules();
  vi.unstubAllEnvs();
  configureApi.mockClear();
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
