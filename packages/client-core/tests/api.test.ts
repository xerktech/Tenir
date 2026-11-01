import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  checkSession,
  describeBootFailure,
  describeLoginError,
  getStatus,
  history,
  meetingSpeakers,
  platformLabel,
  type Conversation,
  login,
  me,
  NetworkError,
  ServerUnavailableError,
  type SystemStatus,
  users,
} from "../src/api";
import {
  clearOidcSession,
  clearToken,
  getOidcSession,
  getToken,
  registerOidcRefresher,
  setOidcSession,
  setToken,
} from "../src/auth";
import { configureApi } from "../src/config";

type FetchCall = { url: string; init: RequestInit };
let calls: FetchCall[];

function mockFetch(responder: (call: FetchCall) => Response): void {
  calls = [];
  globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const call = { url: String(url), init: init ?? {} };
    calls.push(call);
    return responder(call);
  }) as unknown as typeof fetch;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  clearToken();
  configureApi({ httpBaseUrl: "http://gw" });
});

describe("request plumbing", () => {
  it("targets the configured base URL and carries the bearer token", async () => {
    setToken("tok-1");
    mockFetch(() => json([]));
    await history.list();
    expect(calls[0].url).toBe("http://gw/conversations?limit=50&offset=0");
    expect((calls[0].init.headers as Record<string, string>).Authorization).toBe("Bearer tok-1");
  });

  it("throws a typed ApiError carrying the server's detail", async () => {
    mockFetch(() => json({ detail: "conversation not found" }, 404));
    await expect(history.get("ghost")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      message: "conversation not found",
    });
  });

  it("throws NetworkError when the server is unreachable", async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new TypeError("fetch failed");
    }) as unknown as typeof fetch;
    await expect(me()).rejects.toBeInstanceOf(NetworkError);
  });

  it("a 503 is a transient ServerUnavailableError (a NetworkError), not an ApiError", async () => {
    setToken("tok-1");
    mockFetch(() => json({ detail: "the server can't reach its database" }, 503));
    const err = await me().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ServerUnavailableError);
    expect(err).toBeInstanceOf(NetworkError); // callers keep the session, as when unreachable
    expect(err).not.toBeInstanceOf(ApiError);
    expect((err as Error).message).toBe("the server can't reach its database");
    expect(getToken()).toBe("tok-1");
  });

  it.each([502, 504])("a gateway %i is a NetworkError, not an ApiError", async (status) => {
    setToken("tok-1");
    mockFetch(() => new Response("Bad Gateway", { status }));
    const err = await me().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(NetworkError); // callers keep the session, not a login form
    expect(err).not.toBeInstanceOf(ServerUnavailableError);
    expect(err).not.toBeInstanceOf(ApiError);
    expect(getToken()).toBe("tok-1");
  });

  it("returns undefined for a 204 (delete)", async () => {
    mockFetch(() => new Response(null, { status: 204 }));
    await expect(history.remove("c1")).resolves.toBeUndefined();
    expect(calls[0].url).toBe("http://gw/conversations/c1");
    expect(calls[0].init.method).toBe("DELETE");
  });
});

describe("auth", () => {
  it("login stores the token then resolves the principal via /auth/me", async () => {
    mockFetch(({ url }) =>
      url.endsWith("/auth/login")
        ? json({ token: "tok-9" })
        : json({ userId: "u1", username: "maya", household: "acme", role: "admin" }),
    );
    const principal = await login("maya", "pw");
    expect(principal.household).toBe("acme");
    // The /auth/me call carried the token from the login response.
    expect((calls[1].init.headers as Record<string, string>).Authorization).toBe("Bearer tok-9");
  });

  it("adopts a renewed token from the X-Renewed-Token header (XERK-168)", async () => {
    setToken("tok-old");
    mockFetch(
      () =>
        new Response(JSON.stringify({ userId: "u1" }), {
          status: 200,
          headers: { "Content-Type": "application/json", "X-Renewed-Token": "tok-fresh" },
        }),
    );
    await me();
    expect(getToken()).toBe("tok-fresh");
    // The next request already carries the renewed token.
    await me();
    expect((calls[1].init.headers as Record<string, string>).Authorization).toBe(
      "Bearer tok-fresh",
    );
  });

  it("leaves the stored token alone when no renewal header is present", async () => {
    setToken("tok-old");
    mockFetch(() => json({ userId: "u1" }));
    await me();
    expect(getToken()).toBe("tok-old");
  });

  it("describeLoginError maps the three user-facing cases", () => {
    expect(describeLoginError(new NetworkError("nope"))).toMatch(/reach the server/);
    expect(describeLoginError(new ServerUnavailableError("db down"))).toMatch(/reach its database/);
    expect(describeLoginError(new ApiError(401, "unauthorized"))).toMatch(/Incorrect username/);
    expect(describeLoginError(new ApiError(500, "boom"))).toMatch(/Server error \(500\)/);
    expect(describeLoginError(new ApiError(409, "conflict"))).toBe("409: conflict");
  });
});

describe("checkSession (boot, XERK-1591)", () => {
  const PRINCIPAL = { userId: "u1", username: "maya", household: "acme", role: "admin" };

  it("resolves the principal and keeps a good token", async () => {
    setToken("tok-1");
    mockFetch(() => json(PRINCIPAL));
    await expect(checkSession()).resolves.toEqual(PRINCIPAL);
    expect(getToken()).toBe("tok-1");
  });

  it("clears a dead token on 401 and resolves null (login form)", async () => {
    setToken("tok-dead");
    mockFetch(() => json({ detail: "token expired" }, 401));
    await expect(checkSession()).resolves.toBeNull();
    expect(getToken()).toBeNull();
  });

  it("keeps an OIDC refresh sidecar on a 401 so the session recovers once the IdP is back", async () => {
    // The IdP is unreachable: request()'s silent refresh fails, /auth/me 401s.
    setToken("oidc-stale");
    setOidcSession({ refreshToken: "rt-1", expiresAt: 0 });
    registerOidcRefresher(async () => false);
    mockFetch(() => json({ detail: "expired" }, 401));
    await expect(checkSession()).resolves.toBeNull();
    expect(getToken()).toBeNull(); // the dead access token is gone
    expect(getOidcSession()?.refreshToken).toBe("rt-1"); // the refresh token is not

    // Next boot, IdP back: the 401 refreshes and the retry signs straight in.
    registerOidcRefresher(async () => {
      setToken("oidc-fresh");
      return true;
    });
    mockFetch(({ init }) =>
      (init.headers as Record<string, string>).Authorization === "Bearer oidc-fresh"
        ? json(PRINCIPAL)
        : json({ detail: "expired" }, 401),
    );
    await expect(checkSession()).resolves.toEqual(PRINCIPAL);
    registerOidcRefresher(null);
    clearOidcSession();
  });

  it.each([500, 501, 502, 503, 504])(
    "keeps the token and rejects on a %i (server fault, not a verdict on the token)",
    async (status) => {
      setToken("tok-1");
      mockFetch(() => json({ detail: "boom" }, status));
      await expect(checkSession()).rejects.toBeInstanceOf(Error);
      expect(getToken()).toBe("tok-1");
    },
  );

  it("keeps the token and rejects with NetworkError when unreachable", async () => {
    setToken("tok-1");
    globalThis.fetch = vi.fn(async () => {
      throw new TypeError("fetch failed");
    }) as unknown as typeof fetch;
    await expect(checkSession()).rejects.toBeInstanceOf(NetworkError);
    expect(getToken()).toBe("tok-1");
  });

  it("resolves null for a server fault on a first run (no stored token)", async () => {
    mockFetch(() => json({ detail: "boom" }, 500));
    await expect(checkSession()).resolves.toBeNull();
  });

  it("resolves null and keeps the token on a non-auth 4xx", async () => {
    setToken("tok-1");
    mockFetch(() => json({ detail: "nope" }, 403));
    await expect(checkSession()).resolves.toBeNull();
    expect(getToken()).toBe("tok-1");
  });

  it("describeBootFailure words a 5xx as a server error and the rest as unreachable", () => {
    const fault = describeBootFailure(new ApiError(500, "boom"));
    expect(fault.heading).toBe("Your server hit an error");
    expect(fault.body).toMatch(/answered with an error/);
    for (const err of [new NetworkError("down"), new ServerUnavailableError("db")]) {
      const down = describeBootFailure(err);
      expect(down.heading).toBe("Can't reach your server");
      expect(down.body).toMatch(/offline or restarting/);
    }
  });

  it("a 500 on an ordinary action still surfaces as an ApiError", async () => {
    setToken("tok-1");
    mockFetch(() => json({ detail: "boom" }, 500));
    const err = await history.list().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).not.toBeInstanceOf(NetworkError);
    expect((err as ApiError).status).toBe(500);
  });
});

describe("users admin", () => {
  it("list, create and remove target /auth/users", async () => {
    mockFetch(() => json([]));
    await users.list();
    expect(calls[0].url).toBe("http://gw/auth/users");

    mockFetch(() => json({ userId: "u2" }));
    await users.create("ben", "pw", "member");
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(calls[0].init.body as string)).toMatchObject({ username: "ben" });

    mockFetch(() => new Response(null, { status: 204 }));
    await users.remove("u2");
    expect(calls[0].url).toBe("http://gw/auth/users/u2");
  });
});

describe("history", () => {
  it("encodes search + paging params", async () => {
    mockFetch(() => json([]));
    await history.list("budget review", 10, 20);
    expect(calls[0].url).toBe("http://gw/conversations?limit=10&offset=20&q=budget+review");
  });

  it("filters by org and lists the orgs (XERK-2507)", async () => {
    mockFetch(() => json([]));
    await history.list(undefined, 50, 0, "northwind");
    expect(calls[0].url).toBe("http://gw/conversations?limit=50&offset=0&org=northwind");
    await history.orgs();
    expect(calls[1].url).toBe("http://gw/conversations/orgs");
  });

  it("renames a meeting speaker with a PUT (XERK-2507)", async () => {
    mockFetch(() => json({ id: "c1", segments: [], cues: [] }));
    await history.renameSpeaker("c1", "sam@example.com", "Sam");
    expect(calls[0].url).toBe("http://gw/conversations/c1/speakers");
    expect(calls[0].init.method).toBe("PUT");
    expect(JSON.parse(calls[0].init.body as string)).toEqual({
      speaker: "sam@example.com",
      name: "Sam",
    });
  });

  it("lists meeting speakers once, tiles first, and names platforms", () => {
    const conv = {
      participants: ["Priya", "Sam"],
      segments: [{ speaker: "Sam" }, { speaker: null }, { speaker: "Lee" }],
    } as unknown as Conversation;
    expect(meetingSpeakers(conv)).toEqual(["Priya", "Sam", "Lee"]);
    expect(platformLabel("zoom")).toBe("Zoom");
    expect(platformLabel("teams")).toBe("teams");
  });

  it("audioUrl carries the token as a query param for plain navigation", () => {
    setToken("tok/with+chars");
    expect(history.audioUrl("c1")).toBe(
      "http://gw/conversations/c1/audio?token=tok%2Fwith%2Bchars",
    );
    clearToken();
    expect(history.audioUrl("c1")).toBe("http://gw/conversations/c1/audio");
  });
});

describe("status", () => {
  it("getStatus hits the public /status route", async () => {
    const body: SystemStatus = {
      overall: "ready",
      generatedAt: "2026-01-01T00:00:00Z",
      reasons: [],
      components: [],
    };
    mockFetch(() => json(body));
    await expect(getStatus()).resolves.toEqual(body);
    expect(calls[0].url).toBe("http://gw/status");
  });
});

// XERK-237. Tenir is self-hosted, so the server address is a user-typed field
// (mobile Setup/Settings, the web login). That makes a mistyped or
// phished address an ordinary mistake — and it must cost the user nothing more
// than a failed sign-in.
describe("a login must not leak or destroy the existing token", () => {
  it("never sends the bearer token to the server being logged into", async () => {
    setToken("tok-real");
    mockFetch((call) =>
      call.url.endsWith("/auth/login")
        ? json({ token: "tok-new" })
        : json({ userId: "u", username: "ada", household: "h", role: "member" }),
    );

    await login("ada", "pw");

    const loginCall = calls.find((c) => c.url.endsWith("/auth/login"))!;
    const headers = (loginCall.init.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    expect(JSON.stringify(headers)).not.toContain("tok-real");
    // The confirmation call is authenticated, with the NEW token.
    const meCall = calls.find((c) => c.url.endsWith("/auth/me"))!;
    expect((meCall.init.headers as Record<string, string>).Authorization).toBe("Bearer tok-new");
  });

  it("does not adopt a renewed token from a rejected response", async () => {
    setToken("tok-real");
    mockFetch(
      () =>
        new Response(JSON.stringify({ detail: "nope" }), {
          status: 401,
          headers: { "Content-Type": "application/json", "x-renewed-token": "tok-attacker" },
        }),
    );

    await expect(login("ada", "wrong")).rejects.toBeInstanceOf(ApiError);
    expect(getToken()).toBe("tok-real");
  });

  it("restores the previous token when the login is accepted but unconfirmed", async () => {
    setToken("tok-real");
    // Accepts the credentials, then refuses to confirm them.
    mockFetch((call) =>
      call.url.endsWith("/auth/login") ? json({ token: "tok-attacker" }) : json({ detail: "no" }, 401),
    );

    await expect(login("ada", "pw")).rejects.toBeInstanceOf(ApiError);
    expect(getToken()).toBe("tok-real");
  });

  it("leaves no token behind when a first-time login is unconfirmed", async () => {
    clearToken();
    mockFetch((call) =>
      call.url.endsWith("/auth/login") ? json({ token: "tok-attacker" }) : json({ detail: "no" }, 401),
    );

    await expect(login("ada", "pw")).rejects.toBeInstanceOf(ApiError);
    expect(getToken()).toBeNull();
  });

  it("still renews the token from an accepted response (XERK-168)", async () => {
    setToken("tok-1");
    mockFetch(
      () =>
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json", "x-renewed-token": "tok-2" },
        }),
    );

    await history.list();
    expect(getToken()).toBe("tok-2");
  });

  // The api's renewal middleware runs after the route with NO status check, so
  // an aged-but-valid token is renewed on authenticated 404s and 422s too.
  // Gating adoption on `res.ok` would silently drop those (XERK-168/XERK-237).
  it("still renews the token from an authenticated NON-2xx response", async () => {
    setToken("tok-aged");
    mockFetch(
      () =>
        new Response(JSON.stringify({ detail: "not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json", "x-renewed-token": "tok-fresh" },
        }),
    );

    await expect(history.get("gone")).rejects.toBeInstanceOf(ApiError);
    expect(getToken()).toBe("tok-fresh");
  });
});
