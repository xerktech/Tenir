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
import { clearToken, setToken } from "../src/auth";
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
