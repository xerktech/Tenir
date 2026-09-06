import { afterEach, describe, expect, it, vi } from "vitest";

import {
  base64UrlEncode,
  buildNativeOidcPrimitives,
  createPkce,
  makeOidcRedirect,
  parseOidcRedirect,
  randomString,
  type RandomBytes,
} from "../src/lib/oidcPrimitives";

const REDIRECT_URI = "com.xerktech.tenir://auth/callback";

// RFC 7636 Appendix B worked example: these 32 octets encode to the given verifier, whose
// SHA-256 (S256) encodes to the given challenge. Using the spec vector cross-checks both
// base64url encoding and the js-sha256 digest against a known-good result.
const RFC_VERIFIER_BYTES = Uint8Array.from([
  116, 24, 223, 180, 151, 153, 224, 37, 79, 250, 96, 125, 216, 173, 187, 186, 22, 212, 37, 77, 105,
  214, 191, 240, 91, 88, 5, 88, 83, 132, 141, 121,
]);
const RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
const RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

/** A deterministic RandomBytes that yields a fixed buffer (padded/truncated to length). */
function fixedBytes(bytes: Uint8Array): RandomBytes {
  return (length) => {
    const out = new Uint8Array(length);
    out.set(bytes.subarray(0, length));
    return out;
  };
}

describe("base64url encoding", () => {
  it("encodes to url-safe base64 with no padding", () => {
    expect(base64UrlEncode(Uint8Array.from([0]))).toBe("AA");
    expect(base64UrlEncode(Uint8Array.from([0, 0]))).toBe("AAA");
    expect(base64UrlEncode(Uint8Array.from([255, 255, 255]))).toBe("____");
    // 0xFB 0xF0 exercises both of the substituted characters (+ → -, / → _).
    expect(base64UrlEncode(Uint8Array.from([251, 240]))).toBe("-_A");
    expect(base64UrlEncode(RFC_VERIFIER_BYTES)).toBe(RFC_VERIFIER);
  });
});

describe("createPkce", () => {
  it("derives the S256 challenge from the verifier (RFC 7636 vector)", () => {
    const pkce = createPkce(fixedBytes(RFC_VERIFIER_BYTES));
    expect(pkce.verifier).toBe(RFC_VERIFIER);
    expect(pkce.challenge).toBe(RFC_CHALLENGE);
  });
});

describe("randomString", () => {
  it("base64url-encodes 16 random bytes", () => {
    const s = randomString(fixedBytes(Uint8Array.from(Array(16).fill(255))));
    // 16 bytes → 22 base64url chars, no padding; 15×0xFF ⇒ 20 "_", trailing 0xFF ⇒ "_w".
    expect(s).toBe("_".repeat(21) + "w");
    expect(s).toMatch(/^[A-Za-z0-9_-]+$/);
  });
});

describe("parseOidcRedirect", () => {
  it("extracts code + state from our redirect", () => {
    const cb = parseOidcRedirect(`${REDIRECT_URI}?code=abc&state=xyz`, REDIRECT_URI);
    expect(cb).toEqual({ code: "abc", state: "xyz", error: undefined, error_description: undefined });
  });

  it("surfaces an error callback", () => {
    const cb = parseOidcRedirect(
      `${REDIRECT_URI}?error=access_denied&error_description=nope`,
      REDIRECT_URI,
    );
    expect(cb?.error).toBe("access_denied");
    expect(cb?.error_description).toBe("nope");
  });

  it("returns null for a deep link that is not our redirect", () => {
    expect(parseOidcRedirect("com.other.app://x?code=abc", REDIRECT_URI)).toBeNull();
  });
});

describe("makeOidcRedirect", () => {
  afterEach(() => vi.useRealTimers());

  type Handlers = { url?: (u: string) => void; fg?: () => void };

  function deps(handlers: Handlers, openUrl = vi.fn().mockResolvedValue(undefined)) {
    return {
      redirectUri: REDIRECT_URI,
      openUrl,
      onUrl: (h: (u: string) => void) => {
        handlers.url = h;
        return () => {
          handlers.url = undefined;
        };
      },
      onForeground: (h: () => void) => {
        handlers.fg = h;
        return () => {
          handlers.fg = undefined;
        };
      },
      cancelGraceMs: 400,
    };
  }

  it("opens the authorize url and resolves with the callback on redirect", async () => {
    const handlers: Handlers = {};
    const d = deps(handlers);
    const redirect = makeOidcRedirect(d);

    const p = redirect("https://idp/authorize?x=1");
    expect(d.openUrl).toHaveBeenCalledWith("https://idp/authorize?x=1");

    handlers.url?.(`${REDIRECT_URI}?code=C&state=S`);
    await expect(p).resolves.toEqual({
      code: "C",
      state: "S",
      error: undefined,
      error_description: undefined,
    });
    // Listeners are torn down once settled.
    expect(handlers.url).toBeUndefined();
    expect(handlers.fg).toBeUndefined();
  });

  it("ignores unrelated deep links and keeps waiting", async () => {
    const handlers: Handlers = {};
    const redirect = makeOidcRedirect(deps(handlers));
    const p = redirect("https://idp/authorize");

    handlers.url?.("com.other.app://random");
    handlers.url?.(`${REDIRECT_URI}?code=C&state=S`);
    await expect(p).resolves.toMatchObject({ code: "C", state: "S" });
  });

  it("rejects as cancelled when the browser is dismissed without a redirect", async () => {
    vi.useFakeTimers();
    const handlers: Handlers = {};
    const redirect = makeOidcRedirect(deps(handlers));
    const p = redirect("https://idp/authorize");
    // Attach the rejection handler before the grace timer fires, so the (expected)
    // rejection is never momentarily unhandled under fake timers.
    const rejected = expect(p).rejects.toThrow(/cancelled/i);

    handlers.fg?.(); // returned to foreground, no redirect arrived
    await vi.advanceTimersByTimeAsync(400);
    await rejected;
  });

  it("lets a redirect racing the foreground event win over a cancel", async () => {
    vi.useFakeTimers();
    const handlers: Handlers = {};
    const redirect = makeOidcRedirect(deps(handlers));
    const p = redirect("https://idp/authorize");

    handlers.fg?.(); // foreground fires first (browser closing)…
    handlers.url?.(`${REDIRECT_URI}?code=C&state=S`); // …then the redirect lands within the grace window
    await vi.advanceTimersByTimeAsync(400);
    await expect(p).resolves.toMatchObject({ code: "C", state: "S" });
  });

  it("rejects when the browser cannot be opened", async () => {
    const handlers: Handlers = {};
    const openUrl = vi.fn().mockRejectedValue(new Error("no browser"));
    const redirect = makeOidcRedirect(deps(handlers, openUrl));
    await expect(redirect("https://idp/authorize")).rejects.toThrow(/could not open/i);
  });
});

describe("buildNativeOidcPrimitives", () => {
  it("assembles primitives wired to the injected seams", async () => {
    const handlers: { url?: (u: string) => void } = {};
    const primitives = buildNativeOidcPrimitives({
      randomBytes: fixedBytes(RFC_VERIFIER_BYTES),
      redirectUri: REDIRECT_URI,
      openUrl: vi.fn().mockResolvedValue(undefined),
      onUrl: (h) => {
        handlers.url = h;
        return () => {};
      },
      onForeground: () => () => {},
    });

    expect(primitives.redirectUri).toBe(REDIRECT_URI);
    expect(await primitives.createPkce()).toEqual({
      verifier: RFC_VERIFIER,
      challenge: RFC_CHALLENGE,
    });
    expect(primitives.randomString()).toMatch(/^[A-Za-z0-9_-]+$/);

    const p = primitives.redirect("https://idp/authorize");
    handlers.url?.(`${REDIRECT_URI}?code=C&state=S`);
    await expect(p).resolves.toMatchObject({ code: "C", state: "S" });
  });
});
