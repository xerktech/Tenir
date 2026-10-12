/**
 * Two browser tabs over one shared `localStorage` (XERK-1591). Each "tab" is a fresh
 * client-core module instance (its own in-memory state: single-flight refresh, the
 * refresh generation, the OIDC provider), while the token, the sign-in epoch and the
 * OIDC sidecar live in the shared storage — exactly what two real tabs share.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

type Core = typeof import("../src/index");

const PROVIDER = {
  issuer: "https://idp.example/application/o/tenir/",
  clientId: "tenir-client",
  scopes: ["openid", "email", "profile", "groups"],
  authorizationEndpoint: "https://idp.example/application/o/authorize/",
  tokenEndpoint: "https://idp.example/application/o/token/",
  endSessionEndpoint: "https://idp.example/application/o/end-session/",
};

type Call = { url: string; init: RequestInit };
let calls: Call[];

function stubFetch(responder: (call: Call) => Response | Promise<Response>): void {
  calls = [];
  globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const call = { url: String(url), init: init ?? {} };
    calls.push(call);
    return responder(call);
  }) as unknown as typeof fetch;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const auth = (c: Call) => (c.init.headers as Record<string, string> | undefined)?.Authorization;
const grant = (c: Call) => new URLSearchParams(String(c.init.body ?? "")).get("grant_type");
const fakeJwt = (claims: Record<string, unknown>) =>
  `x.${btoa(JSON.stringify(claims)).replace(/=+$/, "")}.y`;

function gate<T>(): { promise: Promise<T>; release: (v: T) => void } {
  let release!: (v: T) => void;
  const promise = new Promise<T>((r) => (release = r));
  return { promise, release };
}

/** A fresh client-core instance over the shared storage, wired like a real tab. */
async function openTab(redirect: () => unknown = () => undefined): Promise<Core> {
  vi.resetModules();
  const core = (await import("../src/index")) as Core;
  core.configureApi({ httpBaseUrl: "http://gw" });
  let n = 0;
  core.configureOidc({
    createPkce: () => ({ verifier: "verifier-xyz", challenge: "challenge-abc" }),
    randomString: () => `s${++n}`,
    redirectUri: "app://oidc/cb",
    redirect: redirect as never,
  });
  core.setOidcProvider(PROVIDER);
  return core;
}

const PRINCIPAL_A = { userId: "uA", username: "ada", household: "h", role: "member" };
const PRINCIPAL_B = { userId: "uB", username: "bo", household: "h", role: "member" };

beforeEach(() => {
  localStorage.clear();
});

describe("cross-tab sign-in epoch", () => {
  it("a request from A in tab 1 is never retried as B after tab 2 logs out and B signs in", async () => {
    const tab1 = await openTab();
    tab1.setToken("access-a");
    tab1.setOidcSession({ refreshToken: "refresh-a", expiresAt: Date.now() + 300_000 });

    const heldA = gate<Response>();
    stubFetch((c) => {
      if (c.url === PROVIDER.tokenEndpoint) {
        return grant(c) === "authorization_code"
          ? json({
              access_token: "access-b",
              refresh_token: "refresh-b",
              id_token: fakeJwt({ nonce: "s2" }),
              expires_in: 300,
            })
          : json({ access_token: "access-b2", refresh_token: "refresh-b2", expires_in: 300 });
      }
      if (auth(c) === "Bearer access-a") return heldA.promise;
      return json(PRINCIPAL_B);
    });

    const pending = tab1.me(); // A's request, in flight in tab 1

    // Tab 2: log A out, then B completes an OIDC sign-in (native redirect shape).
    const tab2 = await openTab(async () => ({ code: "auth-code", state: "s1" }));
    tab2.logout();
    await tab2.startOidcLogin();
    expect(localStorage.getItem("tenir.token")).toBe("access-b");

    heldA.release(json({ detail: "expired" }, 401));
    await expect(pending).rejects.toMatchObject({ status: 401 });
    // No refresh of B's session on A's behalf, and nothing replayed with B's token.
    expect(calls.filter((c) => grant(c) === "refresh_token")).toHaveLength(0);
    expect(calls.filter((c) => c.url.endsWith("/auth/me")).map(auth)).toEqual([
      "Bearer access-a",
      "Bearer access-b", // tab 2's own callback confirmation
    ]);
    expect(localStorage.getItem("tenir.token")).toBe("access-b");
    expect(JSON.parse(localStorage.getItem("tenir.oidc.session") ?? "{}").refreshToken).toBe("refresh-b");
  });

  it("the epoch is stored beside the token and changes on every sign-in and clear", async () => {
    const tab1 = await openTab();
    const tab2 = await openTab();
    const e0 = tab1.tokenGenerations().epoch;
    tab2.clearToken();
    const e1 = localStorage.getItem(tab1.TOKEN_EPOCH_KEY);
    expect(e1).not.toBeNull();
    expect(tab1.tokenGenerations().epoch).toBe(e1); // tab 1 sees tab 2's change
    expect(e1).not.toBe(e0);
    stubFetch((c) => (c.url.endsWith("/auth/login") ? json({ token: "token-b" }) : json(PRINCIPAL_B)));
    await tab2.login("bo", "pw");
    expect(tab1.tokenGenerations().epoch).not.toBe(e1);
  });
});

describe("cross-tab compare-and-clear in checkSession", () => {
  it("never wipes a token another tab refreshed while this tab's check was failing", async () => {
    const tab1 = await openTab();
    tab1.setToken("access-1");
    tab1.setOidcSession({ refreshToken: "refresh-1", expiresAt: 0 });

    // Rotation like Authentik: refresh-1 works once. Tab 1's exchange is held, tab 2's
    // wins; tab 1's then comes back invalid_grant.
    const heldTab1Exchange = gate<Response>();
    let exchanges = 0;
    stubFetch((c) => {
      if (c.url === PROVIDER.tokenEndpoint) {
        exchanges++;
        return exchanges === 1
          ? heldTab1Exchange.promise
          : json({ access_token: "access-2", refresh_token: "refresh-2", expires_in: 300 });
      }
      return auth(c) === "Bearer access-2" ? json(PRINCIPAL_A) : json({ detail: "expired" }, 401);
    });

    const checking = tab1.checkSession(); // tab 1 boots: me(access-1) → 401 → refresh…
    await vi.waitFor(() => expect(exchanges).toBe(1));

    const tab2 = await openTab();
    await tab2.refreshOidcSession(); // tab 2 rotates the shared session meanwhile
    expect(localStorage.getItem("tenir.token")).toBe("access-2");

    heldTab1Exchange.release(json({ error: "invalid_grant" }, 400));
    // Tab 1 must keep tab 2's fresh token, check it, and come up signed in.
    await expect(checking).resolves.toMatchObject({ userId: "uA" });
    expect(localStorage.getItem("tenir.token")).toBe("access-2");
    expect(JSON.parse(localStorage.getItem("tenir.oidc.session") ?? "{}").refreshToken).toBe("refresh-2");
  });

  it("still clears the token it proved dead", async () => {
    const tab1 = await openTab();
    tab1.setToken("dead");
    stubFetch(() => json({ detail: "expired" }, 401));
    await expect(tab1.checkSession()).resolves.toBeNull();
    expect(localStorage.getItem("tenir.token")).toBeNull();
  });
});
