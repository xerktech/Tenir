/**
 * Phone login page flow (XERK-82): cached URL/creds skip the form entirely, a
 * fresh sign-in persists everything to the device store, and the signed-in view
 * embeds the server's own web UI with the token handed over in the fragment.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
    </div>
    <section id="app" hidden>
      <b id="app-user"></b>
      <button id="sign-out" type="button">Log out</button>
      <iframe id="dashboard"></iframe>
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
  it("persists the URL + credentials + token and embeds the web UI signed in", async () => {
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
    // The companion IS the web UI: the server's own origin, token in the fragment.
    expect(els().dashboard.src).toBe("https://tenir.example.com/#token=tok-fresh");
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

  it("boots straight into the signed-in web UI — nothing to re-enter", async () => {
    stubApi();
    const storage = await cachedStorage();
    await cfg.initConfig(storage);
    const onAuthed = vi.fn();
    await loginMod.initPhoneLogin(storage, els(), { onAuthed });

    expect(els().app.hidden).toBe(false);
    expect(els().appUser.textContent).toBe("ada");
    expect(els().dashboard.src).toBe("https://tenir.example.com/#token=tok-cached");
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
    expect(els().dashboard.src).toBe("https://tenir.example.com/#token=tok-renewed");
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

  it("shows the web UI best-effort when the server is unreachable", async () => {
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
    // The signed-out page must not keep an authenticated web UI loaded.
    expect(els().dashboard.src).toBe("about:blank");
  });
});

describe("dashboardUrl", () => {
  it("appends the token as a fragment (never a query/path the server would see)", () => {
    expect(loginMod.dashboardUrl("https://tenir.example.com", "a/b c")).toBe(
      "https://tenir.example.com/#token=a%2Fb%20c",
    );
    expect(loginMod.dashboardUrl("https://tenir.example.com/", null)).toBe("https://tenir.example.com/");
  });
});
