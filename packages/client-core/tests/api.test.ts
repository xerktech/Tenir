import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  describeLoginError,
  getStatus,
  history,
  login,
  me,
  NetworkError,
  type SystemStatus,
  users,
} from "../src/api";
import { clearToken, getToken, setToken } from "../src/auth";
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
    expect(describeLoginError(new ApiError(401, "unauthorized"))).toMatch(/Incorrect username/);
    expect(describeLoginError(new ApiError(500, "boom"))).toMatch(/Server error \(500\)/);
    expect(describeLoginError(new ApiError(409, "conflict"))).toBe("409: conflict");
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
// (mobile Setup/Settings, the web + glasses login). That makes a mistyped or
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
