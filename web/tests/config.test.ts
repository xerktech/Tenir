import { afterEach, describe, expect, it, vi } from "vitest";

// Stub the client-core symbols web/src/config.ts touches so importing the module
// (which configures the REST client as a side effect) is inert.
const { configureApi, configureOidc, browserOidcPrimitives } = vi.hoisted(() => ({
  configureApi: vi.fn(),
  configureOidc: vi.fn(),
  // Returns an opaque primitives bag; config.ts only forwards it to configureOidc.
  browserOidcPrimitives: vi.fn((redirectUri: string) => ({ redirectUri })),
}));
vi.mock("@tenir/client-core", () => ({
  configureApi,
  configureOidc,
  browserOidcPrimitives,
}));

afterEach(() => {
  vi.resetModules();
  vi.unstubAllEnvs();
  configureApi.mockClear();
  configureOidc.mockClear();
  browserOidcPrimitives.mockClear();
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

  it("derives the same-origin OIDC redirect URI and wires the browser primitives", async () => {
    // Production: no dev seed, so the api (and redirect) is the page's own origin.
    vi.stubEnv("VITE_API_HTTP", "");
    const { oidcRedirectUri } = await import("../src/config");
    expect(oidcRedirectUri()).toBe("https://localhost/auth/oidc/callback");
    // On import the SPA registers the browser OIDC primitives (Web Crypto + location)
    // built from that redirect URI, so the optional Authentik path and silent
    // refresh can run (docs/auth-oidc.md §10).
    expect(browserOidcPrimitives).toHaveBeenCalledWith("https://localhost/auth/oidc/callback");
    expect(configureOidc).toHaveBeenCalledWith({ redirectUri: "https://localhost/auth/oidc/callback" });
  });

  it("bases the OIDC redirect URI on the dev api seed when set", async () => {
    vi.stubEnv("VITE_API_HTTP", "http://dev-api:8080/");
    const { oidcRedirectUri } = await import("../src/config");
    expect(oidcRedirectUri()).toBe("http://dev-api:8080/auth/oidc/callback");
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
