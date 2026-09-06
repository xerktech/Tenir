/**
 * Phone login page flow (XERK-82): cached URL/creds skip the form entirely, a
 * fresh sign-in persists everything to the device store, and the signed-in view
 * shows the app's own phone pages (Session/History, XERK-93).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OidcPrimitives } from "@tenir/client-core";

import { OIDC_SESSION_KEY } from "../src/config";
import { SERVER_URL_KEY } from "../src/state/settings";
import { MemStorage } from "./memStorage";

let cfg: typeof import("../src/config");
let loginMod: typeof import("../src/phone/login");
let credsMod: typeof import("../src/state/credentials");
let core: typeof import("@tenir/client-core");

const PRINCIPAL = { userId: "u1", username: "ada", household: "h1", role: "member" };

beforeEach(async () => {
  vi.resetModules();
  cfg = await import("../src/config");
  loginMod = await import("../src/phone/login");
  credsMod = await import("../src/state/credentials");
  core = await import("@tenir/client-core");
  mountDom();
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
  // The OIDC sidecar store write-throughs to localStorage; clear it so an OIDC
  // session/transaction never leaks into the next test.
  try {
    localStorage.clear();
  } catch {
    /* no localStorage in this env */
  }
});

/** The slice of index.html the login controller drives. */
function mountDom(): void {
  document.body.innerHTML = `
    <div id="login">
      <div class="field-error" id="login-error"></div>
      <form id="login-form">
        <input id="server-url" type="text" />
        <input id="username" type="text" />
        <input id="password" type="password" />
        <button id="login-submit" type="submit">Log in</button>
      </form>
      <div id="oidc-section" hidden>
        <button id="oidc-login" type="button">Sign in with Authentik</button>
      </div>
    </div>
    <section id="app" hidden>
      <b id="app-user"></b>
      <button id="sign-out" type="button">Log out</button>
    </section>`;
}

function els() {
  return loginMod.queryPhoneLoginElements();
}

function submitForm(): void {
  els().form.dispatchEvent(new Event("submit", { cancelable: true }));
}

/** fetch stub speaking the api's auth surface. */
function stubApi({ loginStatus = 200, meStatus = 200, token = "tok-1" } = {}) {
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/auth/login")) {
      return loginStatus === 200
        ? new Response(JSON.stringify({ token }), { status: 200 })
        : new Response(JSON.stringify({ detail: "bad credentials" }), { status: loginStatus });
    }
    if (path.endsWith("/auth/me")) {
      return meStatus === 200
        ? new Response(JSON.stringify(PRINCIPAL), { status: 200 })
        : new Response(JSON.stringify({ detail: "token expired" }), { status: meStatus });
    }
    throw new Error(`unexpected fetch: ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("first run (nothing cached)", () => {
  it("opens directly on the login form, tells the lens, and never touches the network", async () => {
    const fetchMock = stubApi();
    const storage = new MemStorage();
    await cfg.initConfig(storage);

    const onAuthed = vi.fn();
    const onSignedOut = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onAuthed, onSignedOut });

    expect(els().login.hidden).toBe(false);
    expect(els().app.hidden).toBe(true);
    expect(onAuthed).not.toHaveBeenCalled();
    // The lens is told it's signed out, so it shows its sign-in prompt instead
    // of implying captions are running (XERK-82).
    expect(onSignedOut).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("signing in", () => {
  it("persists the URL + credentials + token and shows the signed-in app", async () => {
    stubApi({ token: "tok-fresh" });
    const storage = new MemStorage();
    await cfg.initConfig(storage);
    const onAuthed = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onAuthed });

    // Loose input, like the mobile setup screen: a bare host is enough.
    els().server.value = "tenir.example.com";
    els().user.value = " ada ";
    els().password.value = "pw";
    submitForm();

    await vi.waitFor(() => expect(els().app.hidden).toBe(false));
    expect(els().login.hidden).toBe(true);
    expect(els().appUser.textContent).toBe("ada");
    expect(onAuthed).toHaveBeenCalledTimes(1);

    // Everything needed for the next launch is in the device store (XERK-82).
    expect(storage.map.get(SERVER_URL_KEY)).toBe("wss://tenir.example.com/ws");
    expect(storage.map.get(cfg.TOKEN_KEY)).toBe("tok-fresh");
    expect(await credsMod.loadCredentials(storage)).toEqual({ username: "ada", password: "pw" });
    // The password field is cleared once it's cached.
    expect(els().password.value).toBe("");
  });

  it("rejects an unusable server address before any network call", async () => {
    const fetchMock = stubApi();
    const storage = new MemStorage();
    await cfg.initConfig(storage);
    await loginMod.initPhoneLogin(storage, els(), {});

    els().server.value = "wss://";
    submitForm();

    await vi.waitFor(() => expect(els().error.classList.contains("show")).toBe(true));
    expect(els().error.textContent).toContain("server address");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("surfaces wrong credentials and stays on the form without caching them", async () => {
    stubApi({ loginStatus: 401 });
    const storage = new MemStorage();
    await cfg.initConfig(storage);
    const onAuthed = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onAuthed });

    els().server.value = "tenir.example.com";
    els().user.value = "ada";
    els().password.value = "wrong";
    submitForm();

    await vi.waitFor(() => expect(els().error.classList.contains("show")).toBe(true));
    expect(els().error.textContent).toBe("Incorrect username or password.");
    expect(els().login.hidden).toBe(false);
    expect(onAuthed).not.toHaveBeenCalled();
    expect(await credsMod.loadCredentials(storage)).toBeNull();
  });
});

describe("returning user (cached device store)", () => {
  async function cachedStorage(): Promise<MemStorage> {
    const storage = new MemStorage();
    storage.map.set(SERVER_URL_KEY, "wss://tenir.example.com/ws");
    storage.map.set(cfg.TOKEN_KEY, "tok-cached");
    await credsMod.saveCredentials(storage, { username: "ada", password: "pw" });
    return storage;
  }

  it("boots straight into the signed-in app — nothing to re-enter", async () => {
    stubApi();
    const storage = await cachedStorage();
    await cfg.initConfig(storage);
    const onAuthed = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onAuthed });

    expect(els().app.hidden).toBe(false);
    expect(els().appUser.textContent).toBe("ada");
    expect(onAuthed).toHaveBeenCalledTimes(1);
  });

  it("re-logs-in silently when the cached token has expired", async () => {
    // Token-aware stub: the cached token 401s, the renewed one works.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
        const path = String(url);
        if (path.endsWith("/auth/login")) {
          return new Response(JSON.stringify({ token: "tok-renewed" }), { status: 200 });
        }
        const auth = (init?.headers as Record<string, string> | undefined)?.Authorization ?? "";
        return auth === "Bearer tok-renewed"
          ? new Response(JSON.stringify(PRINCIPAL), { status: 200 })
          : new Response(JSON.stringify({ detail: "token expired" }), { status: 401 });
      }),
    );
    const storage = await cachedStorage();
    await cfg.initConfig(storage);
    const onAuthed = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onAuthed });

    expect(els().app.hidden).toBe(false);
    expect(onAuthed).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(storage.map.get(cfg.TOKEN_KEY)).toBe("tok-renewed"));
  });

  it("falls back to the form (username prefilled) when the cached credentials are rejected", async () => {
    stubApi({ meStatus: 401, loginStatus: 401 });
    const storage = await cachedStorage();
    await cfg.initConfig(storage);
    const onAuthed = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onAuthed });

    expect(els().login.hidden).toBe(false);
    // Prefilled as the plain host people type, never the wss:// form (XERK-82).
    expect(els().server.value).toBe("tenir.example.com");
    expect(els().user.value).toBe("ada");
    expect(onAuthed).not.toHaveBeenCalled();
  });

  it("shows the app best-effort when the server is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network down");
      }),
    );
    const storage = await cachedStorage();
    await cfg.initConfig(storage);
    const onAuthed = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onAuthed });

    // Offline with a cached sign-in: don't demand a password nobody can verify.
    expect(els().app.hidden).toBe(false);
    expect(onAuthed).toHaveBeenCalledTimes(1);
  });
});


// ---- OIDC (XERK-656) --------------------------------------------------------

const OIDC = {
  enabled: true,
  issuer: "https://idp.example/application/o/tenir/",
  clientId: "tenir-client",
  authorizationEndpoint: "https://idp.example/application/o/authorize/",
  scopes: ["openid", "email", "profile", "groups"],
};
const DISCOVERY = {
  authorization_endpoint: OIDC.authorizationEndpoint,
  token_endpoint: "https://idp.example/application/o/token/",
  end_session_endpoint: "https://idp.example/application/o/end-session/",
};

const b64url = (o: unknown) =>
  btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
/** A minimal unsigned JWT carrying the given claims (the client only reads `nonce`). */
const fakeJwt = (claims: Record<string, unknown>) =>
  `${b64url({ alg: "RS256", typ: "JWT" })}.${b64url(claims)}.sig`;

/** fetch stub speaking the api auth surface + the IdP discovery/token endpoints. */
function stubOidcApi(
  opts: {
    oidc?: boolean; // whether /auth/config advertises OIDC
    idNonce?: string; // nonce baked into the returned id_token
    onToken?: (form: URLSearchParams) => Response; // override the token response
    me?: (auth: string) => Response; // override /auth/me
  } = {},
) {
  const { oidc = true } = opts;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/auth/config")) {
      const body = oidc ? { builtin: true, oidc: OIDC } : { builtin: true };
      return new Response(JSON.stringify(body), { status: 200 });
    }
    if (path.endsWith("/.well-known/openid-configuration")) {
      return new Response(JSON.stringify(DISCOVERY), { status: 200 });
    }
    if (path === DISCOVERY.token_endpoint) {
      const form = new URLSearchParams(String(init?.body ?? ""));
      if (opts.onToken) return opts.onToken(form);
      return new Response(
        JSON.stringify({
          access_token: "oidc-access-1",
          refresh_token: "oidc-refresh-1",
          id_token: fakeJwt({ nonce: opts.idNonce ?? "s2" }),
          expires_in: 300,
        }),
        { status: 200 },
      );
    }
    if (path.endsWith("/auth/me")) {
      const auth = (init?.headers as Record<string, string> | undefined)?.Authorization ?? "";
      if (opts.me) return opts.me(auth);
      return new Response(JSON.stringify(PRINCIPAL), { status: 200 });
    }
    throw new Error(`unexpected fetch: ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Deterministic OIDC primitives: state=s1, nonce=s2, and `redirect` records the URL. */
function fakePrimitives(rec: { url?: string }): OidcPrimitives {
  let n = 0;
  return {
    createPkce: () => ({ verifier: "verifier-xyz", challenge: "challenge-abc" }),
    randomString: () => `s${(n += 1)}`,
    redirectUri: "https://phone.example/",
    redirect: (url: string) => {
      rec.url = url; // jsdom can't navigate; record where we'd have gone
    },
  };
}

function serverConfigured(extra: (s: MemStorage) => void = () => {}): MemStorage {
  const s = new MemStorage();
  s.map.set(SERVER_URL_KEY, "wss://tenir.example.com/ws");
  extra(s);
  return s;
}

describe("OIDC advertisement", () => {
  it("shows the Authentik button when the configured server advertises OIDC", async () => {
    stubOidcApi({ oidc: true });
    const storage = serverConfigured();
    await cfg.initConfig(storage);
    await loginMod.initPhoneLogin(storage, els(), {});

    // The probe is awaited before boot resolves to the form, so no wait needed.
    expect(els().login.hidden).toBe(false);
    expect(els().oidcSection.hidden).toBe(false);
  });

  it("keeps the button hidden when the server is built-in only", async () => {
    stubOidcApi({ oidc: false });
    const storage = serverConfigured();
    await cfg.initConfig(storage);
    await loginMod.initPhoneLogin(storage, els(), {});

    expect(els().oidcSection.hidden).toBe(true);
  });

  it("reveals the button after the wearer types a server that advertises OIDC", async () => {
    stubOidcApi({ oidc: true });
    const storage = new MemStorage(); // first run: nothing configured
    await cfg.initConfig(storage);
    await loginMod.initPhoneLogin(storage, els(), {});
    expect(els().oidcSection.hidden).toBe(true);

    els().server.value = "tenir.example.com";
    els().server.dispatchEvent(new Event("change"));

    await vi.waitFor(() => expect(els().oidcSection.hidden).toBe(false));
  });
});

describe("OIDC redirect", () => {
  it("persists the server, prepares the provider, and redirects to Authentik", async () => {
    stubOidcApi({ oidc: true });
    const storage = new MemStorage();
    await cfg.initConfig(storage);
    const rec: { url?: string } = {};
    core.configureOidc(fakePrimitives(rec));
    await loginMod.initPhoneLogin(storage, els(), {});

    els().server.value = "tenir.example.com";
    els().oidcButton.click();

    await vi.waitFor(() => expect(rec.url).toBeDefined());
    const authorize = new URL(rec.url!);
    expect(authorize.origin + authorize.pathname).toBe(OIDC.authorizationEndpoint);
    expect(authorize.searchParams.get("client_id")).toBe("tenir-client");
    expect(authorize.searchParams.get("code_challenge")).toBe("challenge-abc");
    expect(authorize.searchParams.get("code_challenge_method")).toBe("S256");
    // The server is persisted so the post-redirect boot points at the same instance.
    expect(storage.map.get(SERVER_URL_KEY)).toBe("wss://tenir.example.com/ws");
    // The PKCE transaction is saved to survive the round-trip.
    expect(core.getOidcTransaction()).toMatchObject({
      verifier: "verifier-xyz",
      state: "s1",
      nonce: "s2",
    });
  });

  it("refuses and explains when the entered server doesn't offer OIDC", async () => {
    stubOidcApi({ oidc: false });
    const storage = new MemStorage();
    await cfg.initConfig(storage);
    const rec: { url?: string } = {};
    core.configureOidc(fakePrimitives(rec));
    await loginMod.initPhoneLogin(storage, els(), {});

    els().server.value = "tenir.example.com";
    els().oidcButton.click();

    await vi.waitFor(() => expect(els().error.classList.contains("show")).toBe(true));
    expect(els().error.textContent).toContain("doesn't offer Authentik");
    expect(rec.url).toBeUndefined();
  });
});

describe("OIDC callback (returning from Authentik)", () => {
  it("completes the exchange, stores the token, and lands in the app", async () => {
    stubOidcApi({ oidc: true, idNonce: "nonce-1" });
    const storage = serverConfigured();
    await cfg.initConfig(storage);
    // The PKCE transaction saved on this device before the redirect.
    core.setOidcTransaction({ verifier: "verifier-xyz", state: "st-1", nonce: "nonce-1" });
    const onAuthed = vi.fn();
    const clearCallbackUrl = vi.fn();

    await loginMod.initPhoneLogin(
      storage,
      els(),
      { onAuthed },
      { callbackSearch: "?code=auth-code&state=st-1", clearCallbackUrl },
    );

    expect(els().app.hidden).toBe(false);
    expect(els().appUser.textContent).toBe("ada");
    expect(onAuthed).toHaveBeenCalledTimes(1);
    expect(core.getToken()).toBe("oidc-access-1");
    expect(core.getSessionKind()).toBe("oidc");
    expect(clearCallbackUrl).toHaveBeenCalledTimes(1);
    // The refresh token + expiry persist to the device store (survive a restart).
    await vi.waitFor(() => expect(storage.map.has(OIDC_SESSION_KEY)).toBe(true));
  });

  it("drops back to the form with the reason when the callback carries an error", async () => {
    stubOidcApi({ oidc: true });
    const storage = serverConfigured();
    await cfg.initConfig(storage);
    const onSignedOut = vi.fn();

    await loginMod.initPhoneLogin(
      storage,
      els(),
      { onSignedOut },
      {
        callbackSearch: "?error=access_denied&error_description=User%20cancelled&state=st",
        clearCallbackUrl: () => {},
      },
    );

    expect(els().login.hidden).toBe(false);
    expect(els().error.classList.contains("show")).toBe(true);
    expect(els().error.textContent).toContain("User cancelled");
    expect(onSignedOut).toHaveBeenCalledTimes(1);
    expect(core.getToken()).toBeNull();
  });

  it("silently refreshes an expired OIDC token on boot", async () => {
    // Cached OIDC session, but the access token is stale: me() 401s with the old
    // token and succeeds once it's been refreshed.
    stubOidcApi({
      oidc: true,
      onToken: () =>
        new Response(JSON.stringify({ access_token: "oidc-access-2", expires_in: 300 }), {
          status: 200,
        }),
      me: (auth) =>
        auth === "Bearer oidc-access-2"
          ? new Response(JSON.stringify(PRINCIPAL), { status: 200 })
          : new Response(JSON.stringify({ detail: "expired" }), { status: 401 }),
    });
    const storage = serverConfigured((s) => {
      s.map.set(cfg.TOKEN_KEY, "oidc-access-stale");
      s.map.set(
        OIDC_SESSION_KEY,
        JSON.stringify({ refreshToken: "oidc-refresh-1", expiresAt: Date.now() - 1000 }),
      );
    });
    await cfg.initConfig(storage);
    const onAuthed = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onAuthed });

    expect(els().app.hidden).toBe(false);
    expect(onAuthed).toHaveBeenCalledTimes(1);
    expect(core.getToken()).toBe("oidc-access-2");
  });
});

describe("OIDC sign-out", () => {
  it("clears the token + session sidecar and returns to the form", async () => {
    const rec: { url?: string } = {};
    stubOidcApi({ oidc: true });
    const storage = serverConfigured((s) => {
      s.map.set(cfg.TOKEN_KEY, "oidc-access-1");
      s.map.set(
        OIDC_SESSION_KEY,
        JSON.stringify({ refreshToken: "r", expiresAt: Date.now() + 60_000 }),
      );
    });
    await cfg.initConfig(storage);
    core.configureOidc(fakePrimitives(rec)); // record the end_session redirect
    const onSignedOut = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onSignedOut });
    expect(els().app.hidden).toBe(false); // valid token → straight into the app
    expect(core.getSessionKind()).toBe("oidc");

    els().signOut.click();

    await vi.waitFor(() => expect(els().login.hidden).toBe(false));
    expect(onSignedOut).toHaveBeenCalledTimes(1);
    expect(core.getToken()).toBeNull();
    expect(core.getSessionKind()).toBe("builtin"); // sidecar cleared
    await vi.waitFor(() => expect(storage.map.has(OIDC_SESSION_KEY)).toBe(false));
    // RP-initiated logout hit Authentik's end-session endpoint.
    expect(rec.url).toContain(DISCOVERY.end_session_endpoint);
  });
});

describe("signing out", () => {
  it("clears the token + credentials and returns to the form", async () => {
    stubApi();
    const storage = new MemStorage();
    storage.map.set(SERVER_URL_KEY, "wss://tenir.example.com/ws");
    storage.map.set(cfg.TOKEN_KEY, "tok-cached");
    await credsMod.saveCredentials(storage, { username: "ada", password: "pw" });
    await cfg.initConfig(storage);
    const onSignedOut = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onSignedOut });
    expect(els().app.hidden).toBe(false);

    els().signOut.click();

    await vi.waitFor(() => expect(els().login.hidden).toBe(false));
    expect(els().app.hidden).toBe(true);
    expect(onSignedOut).toHaveBeenCalledTimes(1);
    expect(core.getToken()).toBeNull();
    await vi.waitFor(() => expect(storage.map.has(cfg.TOKEN_KEY)).toBe(false));
    expect(await credsMod.loadCredentials(storage)).toBeNull();
  });
});
