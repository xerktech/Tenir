import { beforeEach, describe, expect, it, vi } from "vitest";

import { probeOidcAvailable, signInWithOidc } from "../src/lib/oidc";

const api = vi.hoisted(() => ({
  getAuthConfig: vi.fn(),
  prepareOidc: vi.fn(),
  startOidcLogin: vi.fn(),
}));

vi.mock("@tenir/client-core", () => ({
  getAuthConfig: api.getAuthConfig,
  prepareOidc: api.prepareOidc,
  startOidcLogin: api.startOidcLogin,
}));

beforeEach(() => vi.clearAllMocks());

describe("probeOidcAvailable", () => {
  it("resolves the provider and returns true when the server advertises OIDC", async () => {
    const oidc = { enabled: true, issuer: "https://idp/", clientId: "tenir" };
    api.getAuthConfig.mockResolvedValue({ builtin: true, oidc });
    api.prepareOidc.mockResolvedValue({});

    await expect(probeOidcAvailable()).resolves.toBe(true);
    expect(api.prepareOidc).toHaveBeenCalledWith(oidc);
  });

  it("returns false and prepares nothing when OIDC is absent", async () => {
    api.getAuthConfig.mockResolvedValue({ builtin: true });
    await expect(probeOidcAvailable()).resolves.toBe(false);
    expect(api.prepareOidc).not.toHaveBeenCalled();
  });

  it("returns false when OIDC is present but disabled", async () => {
    api.getAuthConfig.mockResolvedValue({
      builtin: true,
      oidc: { enabled: false, issuer: "https://idp/", clientId: "tenir" },
    });
    await expect(probeOidcAvailable()).resolves.toBe(false);
    expect(api.prepareOidc).not.toHaveBeenCalled();
  });

  it("propagates a probe failure to the caller", async () => {
    api.getAuthConfig.mockRejectedValue(new Error("unreachable"));
    await expect(probeOidcAvailable()).rejects.toThrow("unreachable");
  });
});

describe("signInWithOidc", () => {
  it("drives the client-core OIDC login flow", async () => {
    api.startOidcLogin.mockResolvedValue({ userId: "u", username: "ada", household: "h", role: "member" });
    await expect(signInWithOidc()).resolves.toBeUndefined();
    expect(api.startOidcLogin).toHaveBeenCalledTimes(1);
  });

  it("surfaces a failed/cancelled login", async () => {
    api.startOidcLogin.mockRejectedValue(new Error("Sign-in was cancelled"));
    await expect(signInWithOidc()).rejects.toThrow(/cancelled/i);
  });
});
