import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getAuthConfig, me } from "../src/api";
import {
  clearOidcSession,
  clearOidcTransaction,
  clearToken,
  configureOidcStore,
  getOidcSession,
  getSessionKind,
  getToken,
  registerOidcRefresher,
  setOidcSession,
  setToken,
  type OidcStore,
} from "../src/auth";
import { configureApi } from "../src/config";
import {
  browserOidcPrimitives,
  buildAuthorizeUrl,
  completeOidcCallback,
  configureOidc,
  discoverOidc,
  ensureFreshOidcToken,
  oidcLogout,
  OidcError,
  oidcReady,
  oidcTokenDue,
  type OidcPrimitives,
  type OidcProvider,
  prepareOidc,
  refreshOidcSession,
  setOidcProvider,
  startOidcLogin,
} from "../src/oidc";

// ---- test doubles -----------------------------------------------------------

type FetchCall = { url: string; init: RequestInit };
let calls: FetchCall[];

/** Route a mock fetch by URL; the responder returns a Response (or throws for a network error). */
function mockFetch(responder: (call: FetchCall) => Response): void {
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

function form(call: FetchCall): URLSearchParams {
  return new URLSearchParams(String(call.init.body ?? ""));
}

/** A minimal unsigned JWT whose payload carries the given claims (we only read nonce). */
function fakeJwt(claims: Record<string, unknown>): string {
  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "RS256", typ: "JWT" })}.${b64(claims)}.sig`;
}

const PROVIDER: OidcProvider = {
  issuer: "https://idp.example/application/o/tenir/",
  clientId: "tenir-client",
  scopes: ["openid", "email", "profile", "groups"],
  authorizationEndpoint: "https://idp.example/application/o/authorize/",
  tokenEndpoint: "https://idp.example/application/o/token/",
  endSessionEndpoint: "https://idp.example/application/o/end-session/",
};

/** Deterministic primitives: `randomString` yields s1, s2, … so state/nonce are predictable. */
function fakePrimitives(
  redirect: OidcPrimitives["redirect"],
  seq = { n: 0 },
): OidcPrimitives {
  return {
    createPkce: () => ({ verifier: "verifier-xyz", challenge: "challenge-abc" }),
    randomString: () => `s${++seq.n}`,
    redirectUri: "app://oidc/cb",
    redirect,
  };
}

// An in-memory OidcStore so the sidecar doesn't leak between tests.
function memoryOidcStore(): OidcStore {
  const m = new Map<string, string>();
  return {
    get: (k) => m.get(k) ?? null,
    set: (k, v) => void m.set(k, v),
    remove: (k) => void m.delete(k),
  };
}

beforeEach(() => {
  configureApi({ httpBaseUrl: "http://gw" });
  configureOidcStore(memoryOidcStore());
  clearToken();
  clearOidcSession();
  clearOidcTransaction();
  registerOidcRefresher(null);
  setOidcProvider(null as unknown as OidcProvider); // reset provider between tests
});

afterEach(() => {
  registerOidcRefresher(null);
});

// ---- server advertisement (button hidden vs shown) --------------------------

describe("server OIDC advertisement", () => {
  it("hides the OIDC button and stays built-in-only when the server has OIDC off", async () => {
    mockFetch(() => json({ builtin: true }));
    const cfg = await getAuthConfig();
    expect(cfg.builtin).toBe(true);
    expect(cfg.oidc).toBeUndefined();
    // /auth/config is public: no Authorization header attached.
    expect((calls[0].init.headers as Record<string, string>)?.Authorization).toBeUndefined();
    // With no provider prepared, the flow refuses to start — nothing to show a button for.
    expect(oidcReady()).toBe(false);
  });

  it("advertises OIDC when the server has it enabled", async () => {
    mockFetch(() =>
      json({
        builtin: true,
        oidc: { enabled: true, issuer: PROVIDER.issuer, clientId: PROVIDER.clientId },
      }),
    );
    const cfg = await getAuthConfig();
    expect(cfg.oidc?.enabled).toBe(true);
    expect(cfg.oidc?.clientId).toBe("tenir-client");
  });
});

// ---- PKCE challenge + authorize URL -----------------------------------------

describe("PKCE + authorize URL", () => {
  it("builds an authorize URL with PKCE S256, state and nonce", () => {
    const url = new URL(
      buildAuthorizeUrl(PROVIDER, "app://oidc/cb", {
        challenge: "challenge-abc",
        state: "st",
        nonce: "no",
      }),
    );
    expect(url.origin + url.pathname).toBe("https://idp.example/application/o/authorize/");
    const q = url.searchParams;
    expect(q.get("response_type")).toBe("code");
    expect(q.get("client_id")).toBe("tenir-client");
    expect(q.get("redirect_uri")).toBe("app://oidc/cb");
    expect(q.get("scope")).toBe("openid email profile groups");
    expect(q.get("state")).toBe("st");
    expect(q.get("nonce")).toBe("no");
    expect(q.get("code_challenge")).toBe("challenge-abc");
    expect(q.get("code_challenge_method")).toBe("S256");
  });

  it("browser primitives derive the S256 challenge from the verifier (RFC 7636 vector)", async () => {
    // Force the RFC 7636 Appendix B verifier so we can assert the known challenge.
    const verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
    const bytes = Uint8Array.from(atob(verifier.replace(/-/g, "+").replace(/_/g, "/")), (c) =>
      c.charCodeAt(0),
    );
    const spy = vi
      .spyOn(globalThis.crypto, "getRandomValues")
      .mockImplementationOnce((<T extends ArrayBufferView>(a: T) => {
        new Uint8Array((a as unknown as Uint8Array).buffer).set(bytes);
        return a;
      }) as typeof globalThis.crypto.getRandomValues);
    const pkce = await browserOidcPrimitives("app://cb").createPkce();
    spy.mockRestore();
    expect(pkce.verifier).toBe(verifier);
    expect(pkce.challenge).toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  });
});

// ---- full native flow: start → callback → tokens → me → API -----------------

describe("Authorization Code + PKCE flow", () => {
  function wireExchange(over: Partial<Record<string, unknown>> = {}) {
    mockFetch((call) => {
      if (call.url === PROVIDER.tokenEndpoint) {
        return json({
          access_token: "access-1",
          refresh_token: "refresh-1",
          id_token: fakeJwt({ nonce: "s2" }),
          expires_in: 300,
          ...over,
        });
      }
      if (call.url.endsWith("/auth/me")) {
        return json({ userId: "u1", username: "ada", household: "h", role: "member" });
      }
      return json({ detail: "unexpected" }, 500);
    });
  }

  it("completes the whole flow inline for the native redirect shape", async () => {
    setOidcProvider(PROVIDER);
    // Native AppAuth: redirect resolves with the callback code+state (state is s1).
    configureOidc(fakePrimitives(async () => ({ code: "auth-code", state: "s1" })));
    wireExchange();

    const principal = await startOidcLogin();

    expect(principal).toMatchObject({ userId: "u1", household: "h", role: "member" });
    // Access token stored (shape-agnostic) and the session is now an OIDC one.
    expect(getToken()).toBe("access-1");
    expect(getSessionKind()).toBe("oidc");
    expect(getOidcSession()).toMatchObject({ refreshToken: "refresh-1" });

    // The token exchange sent the PKCE verifier + code + redirect_uri + client_id.
    const exchange = form(calls.find((c) => c.url === PROVIDER.tokenEndpoint)!);
    expect(exchange.get("grant_type")).toBe("authorization_code");
    expect(exchange.get("code")).toBe("auth-code");
    expect(exchange.get("code_verifier")).toBe("verifier-xyz");
    expect(exchange.get("redirect_uri")).toBe("app://oidc/cb");
    expect(exchange.get("client_id")).toBe("tenir-client");
    // The confirming /auth/me carried the new access token.
    const meCall = calls.find((c) => c.url.endsWith("/auth/me"))!;
    expect((meCall.init.headers as Record<string, string>).Authorization).toBe("Bearer access-1");
  });

  it("supports the browser redirect shape: start navigates, callback completes later", async () => {
    setOidcProvider(PROVIDER);
    let navigated = "";
    configureOidc(fakePrimitives((url) => void (navigated = url as string)));
    wireExchange();

    const inline = await startOidcLogin();
    expect(inline).toBeUndefined(); // browser navigated away; nothing resolved inline
    expect(navigated).toContain("code_challenge=challenge-abc");

    // The callback screen finishes the flow from window.location.search.
    const principal = await completeOidcCallback("?code=auth-code&state=s1");
    expect(principal.userId).toBe("u1");
    expect(getToken()).toBe("access-1");
  });

  it("rejects a state mismatch (CSRF guard) and stores no token", async () => {
    setOidcProvider(PROVIDER);
    configureOidc(fakePrimitives(() => undefined));
    mockFetch(() => json({ detail: "should not be called" }, 500));

    await startOidcLogin(); // persists tx with state s1
    await expect(completeOidcCallback({ code: "c", state: "WRONG" })).rejects.toBeInstanceOf(
      OidcError,
    );
    expect(getToken()).toBeNull();
    // The token endpoint was never hit.
    expect(calls.some((c) => c.url === PROVIDER.tokenEndpoint)).toBe(false);
  });

  it("surfaces an IdP error returned on the callback", async () => {
    setOidcProvider(PROVIDER);
    configureOidc(fakePrimitives(() => undefined));
    mockFetch(() => json({}, 500));
    await startOidcLogin();
    await expect(
      completeOidcCallback({ error: "access_denied", error_description: "user said no" }),
    ).rejects.toMatchObject({ name: "OidcError", code: "access_denied" });
  });

  it("rejects an id_token whose nonce does not match the login", async () => {
    setOidcProvider(PROVIDER);
    configureOidc(fakePrimitives(async () => ({ code: "c", state: "s1" })));
    wireExchange({ id_token: fakeJwt({ nonce: "attacker" }) });
    await expect(startOidcLogin()).rejects.toMatchObject({ name: "OidcError" });
    expect(getToken()).toBeNull();
  });

  it("rejects a code exchange whose token response omits the id_token", async () => {
    setOidcProvider(PROVIDER);
    configureOidc(fakePrimitives(async () => ({ code: "c", state: "s1" })));
    // openid scope was requested but the IdP returned no id_token → nonce unbindable.
    mockFetch((call) =>
      call.url === PROVIDER.tokenEndpoint
        ? json({ access_token: "a", refresh_token: "r", expires_in: 300 })
        : json({ userId: "u1" }),
    );
    await expect(startOidcLogin()).rejects.toMatchObject({ name: "OidcError" });
    expect(getToken()).toBeNull();
    expect(calls.some((c) => c.url.endsWith("/auth/me"))).toBe(false); // never got that far
  });

  it("clears the half-session if /auth/me rejects the fresh token", async () => {
    setOidcProvider(PROVIDER);
    configureOidc(fakePrimitives(async () => ({ code: "c", state: "s1" })));
    mockFetch((call) =>
      call.url === PROVIDER.tokenEndpoint
        ? json({ access_token: "a", refresh_token: "r", id_token: fakeJwt({ nonce: "s2" }), expires_in: 300 })
        : json({ detail: "no" }, 401),
    );
    await expect(startOidcLogin()).rejects.toBeInstanceOf(ApiError);
    expect(getToken()).toBeNull();
    expect(getSessionKind()).toBe("builtin");
  });
});

// ---- silent refresh ---------------------------------------------------------

describe("silent refresh before expiry", () => {
  beforeEach(() => setOidcProvider(PROVIDER));

  it("oidcTokenDue is true only within the skew window of expiry", () => {
    setOidcSession({ refreshToken: "r", expiresAt: 1_000_000 });
    expect(oidcTokenDue(500_000)).toBe(false); // far from expiry
    expect(oidcTokenDue(1_000_000 - 30_000)).toBe(true); // inside the 60s skew
    expect(oidcTokenDue(1_100_000)).toBe(true); // already expired
  });

  it("refreshOidcSession swaps in a new access token and keeps a non-rotated refresh token", async () => {
    setOidcSession({ refreshToken: "refresh-1", expiresAt: Date.now() + 1000 });
    // IdP returns a new access token but no new refresh token → keep the old one.
    mockFetch((call) => {
      expect(call.url).toBe(PROVIDER.tokenEndpoint);
      expect(form(call).get("grant_type")).toBe("refresh_token");
      expect(form(call).get("refresh_token")).toBe("refresh-1");
      return json({ access_token: "access-2", expires_in: 300 });
    });
    await refreshOidcSession();
    expect(getToken()).toBe("access-2");
    expect(getOidcSession()?.refreshToken).toBe("refresh-1");
  });

  it("ensureFreshOidcToken refreshes a due token and no-ops a fresh one", async () => {
    let hits = 0;
    mockFetch(() => {
      hits++;
      return json({ access_token: "access-fresh", refresh_token: "refresh-2", expires_in: 300 });
    });
    // Fresh token: no refresh.
    setOidcSession({ refreshToken: "r", expiresAt: Date.now() + 3_600_000 });
    await ensureFreshOidcToken();
    expect(hits).toBe(0);
    // Due token: refreshes.
    setOidcSession({ refreshToken: "r", expiresAt: Date.now() + 1000 });
    await ensureFreshOidcToken();
    expect(hits).toBe(1);
    expect(getToken()).toBe("access-fresh");
    expect(getOidcSession()?.refreshToken).toBe("refresh-2");
  });

  it("ensureFreshOidcToken is a no-op for a built-in session", async () => {
    clearOidcSession(); // built-in
    mockFetch(() => json({}, 500));
    await ensureFreshOidcToken();
    expect(calls).toEqual([]); // fetch never invoked
  });
});

// ---- 401 → refresh → retry (REST) -------------------------------------------

describe("401 → refresh → retry", () => {
  beforeEach(() => {
    setOidcProvider(PROVIDER);
    configureOidc(fakePrimitives(() => undefined));
    setOidcSession({ refreshToken: "refresh-1", expiresAt: Date.now() + 1000 });
  });

  it("refreshes on a 401 and retries the request with the new token", async () => {
    let meHits = 0;
    mockFetch((call) => {
      if (call.url === PROVIDER.tokenEndpoint) {
        return json({ access_token: "access-2", refresh_token: "refresh-2", expires_in: 300 });
      }
      // First /auth/me 401s (expired), the retry after refresh succeeds.
      meHits++;
      return meHits === 1
        ? json({ detail: "expired" }, 401)
        : json({ userId: "u1", username: "ada", household: "h", role: "member" });
    });

    const principal = await me();
    expect(principal.userId).toBe("u1");
    expect(getToken()).toBe("access-2");
    // The retried /auth/me carried the refreshed token.
    const meCalls = calls.filter((c) => c.url.endsWith("/auth/me"));
    expect(meCalls).toHaveLength(2);
    expect((meCalls[1].init.headers as Record<string, string>).Authorization).toBe("Bearer access-2");
  });

  it("surfaces the 401 as re-login when the refresh itself fails", async () => {
    mockFetch((call) =>
      call.url === PROVIDER.tokenEndpoint
        ? json({ error: "invalid_grant" }, 400) // refresh token revoked
        : json({ detail: "expired" }, 401),
    );
    await expect(me()).rejects.toMatchObject({ name: "ApiError", status: 401 });
  });

  it("does not attempt refresh for a built-in 401 (no refresher / built-in session)", async () => {
    clearOidcSession(); // built-in session
    mockFetch(() => json({ detail: "nope" }, 401));
    await expect(me()).rejects.toMatchObject({ name: "ApiError", status: 401 });
    // Exactly one /auth/me and no token-endpoint hit.
    expect(calls.filter((c) => c.url.endsWith("/auth/me"))).toHaveLength(1);
    expect(calls.some((c) => c.url === PROVIDER.tokenEndpoint)).toBe(false);
  });
});

// ---- logout -----------------------------------------------------------------

describe("logout", () => {
  it("clears local state and hits the IdP end-session endpoint (RP-initiated)", async () => {
    setOidcProvider(PROVIDER);
    let ended = "";
    configureOidc(fakePrimitives((url) => void (ended = url as string)));
    setOidcSession({ refreshToken: "r", expiresAt: Date.now() + 1000 });
    setToken("access-1");

    await oidcLogout();

    expect(getToken()).toBeNull();
    expect(getSessionKind()).toBe("builtin");
    const u = new URL(ended);
    expect(u.origin + u.pathname).toBe("https://idp.example/application/o/end-session/");
    expect(u.searchParams.get("client_id")).toBe("tenir-client");
    expect(u.searchParams.get("post_logout_redirect_uri")).toBe("app://oidc/cb");
  });

  it("logs out locally even when the provider advertises no end-session endpoint", async () => {
    setOidcProvider({ ...PROVIDER, endSessionEndpoint: undefined });
    configureOidc(fakePrimitives(() => expect.unreachable("must not redirect")));
    setOidcSession({ refreshToken: "r", expiresAt: 1 });
    setToken("access-1");
    await oidcLogout();
    expect(getToken()).toBeNull();
  });
});

// ---- discovery / prepareOidc ------------------------------------------------

describe("prepareOidc (discovery)", () => {
  it("merges the server advertisement with the IdP discovery document", async () => {
    mockFetch((call) => {
      expect(call.url).toBe(`${PROVIDER.issuer}.well-known/openid-configuration`);
      return json({
        authorization_endpoint: PROVIDER.authorizationEndpoint,
        token_endpoint: PROVIDER.tokenEndpoint,
        end_session_endpoint: PROVIDER.endSessionEndpoint,
      });
    });
    const prov = await prepareOidc({
      enabled: true,
      issuer: PROVIDER.issuer,
      clientId: PROVIDER.clientId,
    });
    expect(prov.tokenEndpoint).toBe(PROVIDER.tokenEndpoint);
    expect(prov.authorizationEndpoint).toBe(PROVIDER.authorizationEndpoint);
    expect(prov.scopes).toEqual(["openid", "email", "profile", "groups"]);
  });

  it("throws when discovery lacks a token endpoint", async () => {
    mockFetch(() => json({ authorization_endpoint: PROVIDER.authorizationEndpoint }));
    await expect(
      prepareOidc({ enabled: true, issuer: PROVIDER.issuer, clientId: PROVIDER.clientId }),
    ).rejects.toBeInstanceOf(OidcError);
  });

  it("discoverOidc appends the well-known path to the issuer", async () => {
    mockFetch(() => json({ token_endpoint: "t" }));
    await discoverOidc("https://idp.example/o/tenir"); // no trailing slash
    expect(calls[0].url).toBe("https://idp.example/o/tenir/.well-known/openid-configuration");
  });
});
