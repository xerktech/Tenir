/**
 * Phone-side login page (index.html) — plain DOM, no framework (XERK-82).
 *
 * This is what the wearer sees on their phone while the glasses app runs; it
 * mirrors the web UI's login (web/src/App.tsx) plus the Server field the mobile
 * setup screen has (Tenir is self-hosted, so the api URL is user-entered — the
 * one deliberate difference from the web login, which is served *by* the server).
 *
 * On sign-in it applies + persists the server URL, logs in through the shared
 * `@tenir/client-core` client (which stores the bearer token in the device
 * store), and caches the credentials so the app can re-login silently when the
 * token expires — URL and creds are entered once, ever. The signed-in view then
 * shows the app's own phone pages — Session and History (XERK-93) — which talk
 * to the api through the same signed-in client.
 *
 * Optional Authentik OIDC (XERK-656): when the entered server advertises OIDC
 * (`getAuthConfig().oidc.enabled`) a "Sign in with Authentik" button appears
 * beside the username/password form. It runs the Authorization Code + PKCE flow
 * from client-core — a full-page redirect to the IdP and back to this same page,
 * where the `?code=…&state=…` callback is completed on boot. The access token
 * lands in the SAME device token store the built-in path uses, so the glasses'
 * WS/REST calls are authenticated identically regardless of login method; the
 * OIDC session silently *refreshes* (there are no cached credentials to replay),
 * while the built-in path keeps its cached-credentials silent re-login.
 *
 * Everything is driven through injected elements/callbacks so it unit-tests
 * under jsdom without the Even SDK.
 */

import {
  ApiError,
  completeOidcCallback,
  configureApi,
  describeLoginError,
  displayServerUrl,
  getAuthConfig,
  getSessionKind,
  getToken,
  httpBaseFromWs,
  login,
  logout,
  me,
  normalizeServerUrl,
  OidcError,
  oidcLogout,
  prepareOidc,
  refreshOidcSession,
  startOidcLogin,
  type Principal,
  type ServerAuthConfig,
} from "@tenir/client-core";

import { applyServerUrl, config, isServerConfigured } from "../config";
import { clearCredentials, loadCredentials, saveCredentials, silentLogin } from "../state/credentials";
import type { KeyValueStorage } from "../state/storage";

type ServerOidc = NonNullable<ServerAuthConfig["oidc"]>;

export interface PhoneLoginElements {
  login: HTMLElement; // the login card wrapper
  app: HTMLElement; // the signed-in view (Session/History pages + bottom nav)
  form: HTMLFormElement;
  server: HTMLInputElement;
  user: HTMLInputElement;
  password: HTMLInputElement;
  submit: HTMLButtonElement;
  error: HTMLElement;
  signOut: HTMLButtonElement;
  appUser: HTMLElement;
  /** "Sign in with Authentik" — shown only when the server advertises OIDC. */
  oidcButton: HTMLButtonElement;
  /** Wrapper around the OIDC divider + button, toggled as a unit. */
  oidcSection: HTMLElement;
}

export function queryPhoneLoginElements(doc: Document = document): PhoneLoginElements {
  const byId = <T extends HTMLElement>(id: string): T => {
    const el = doc.getElementById(id);
    if (!el) throw new Error(`phone login: missing #${id}`);
    return el as T;
  };
  return {
    login: byId("login"),
    app: byId("app"),
    form: byId<HTMLFormElement>("login-form"),
    server: byId<HTMLInputElement>("server-url"),
    user: byId<HTMLInputElement>("username"),
    password: byId<HTMLInputElement>("password"),
    submit: byId<HTMLButtonElement>("login-submit"),
    error: byId("login-error"),
    signOut: byId<HTMLButtonElement>("sign-out"),
    appUser: byId("app-user"),
    oidcButton: byId<HTMLButtonElement>("oidc-login"),
    oidcSection: byId("oidc-section"),
  };
}

export interface PhoneLoginCallbacks {
  /** Signed in (at boot or via the form): the lens may start/resume captioning. */
  onAuthed?: () => void;
  /**
   * Not signed in — fired both when boot resolves to the login form and on an
   * explicit sign-out, so the lens shows its sign-in prompt instead of
   * pretending to run (XERK-82).
   */
  onSignedOut?: () => void;
}

/**
 * Environment seams for the OIDC redirect round-trip, injected so the flow
 * unit-tests without a real browser navigation.
 */
export interface PhoneLoginDeps {
  /**
   * The query string carrying an OIDC callback (`?code=…&state=…` or `?error=…`).
   * Defaults to `window.location.search`; boot completes it when present.
   */
  callbackSearch?: string;
  /**
   * Strip the OIDC params from the address bar once the callback is handled, so a
   * reload doesn't replay a spent code. Defaults to a `history.replaceState` to
   * the bare path.
   */
  clearCallbackUrl?: () => void;
}

function showError(els: PhoneLoginElements, msg: string): void {
  els.error.textContent = msg;
  els.error.classList.add("show");
}

/** A friendly message for either a built-in (`ApiError`/network) or OIDC failure. */
function describeAuthError(err: unknown): string {
  if (err instanceof OidcError) return err.message;
  return describeLoginError(err);
}

function showApp(els: PhoneLoginElements, username: string): void {
  els.appUser.textContent = username;
  els.login.hidden = true;
  els.app.hidden = false;
}

function showLogin(els: PhoneLoginElements): void {
  els.login.hidden = false;
  els.app.hidden = true;
}

/** Whether a query string carries an OIDC redirect callback we should complete. */
function hasOidcCallback(search: string): boolean {
  const q = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  return q.has("state") && (q.has("code") || q.has("error"));
}

/**
 * Ask the (already-pointed) server what auth backends it advertises, returning
 * its OIDC block only when OIDC is actually enabled. Never throws — an
 * unreachable server or a deployment with OIDC off simply yields null (built-in
 * only), so the button stays hidden.
 */
async function advertisedOidc(): Promise<ServerOidc | null> {
  try {
    const cfg = await getAuthConfig();
    return cfg.oidc?.enabled ? cfg.oidc : null;
  } catch {
    return null;
  }
}

function renderOidcSection(els: PhoneLoginElements, oidc: ServerOidc | null): void {
  els.oidcSection.hidden = oidc === null;
}

/**
 * Resolve the boot state: with a configured server, try the cached token
 * (`me()`), then — by session kind — an OIDC silent refresh or a built-in silent
 * re-login. Returns the principal when signed in, "offline" when the server
 * can't be reached but a cached sign-in exists (show the app best-effort — the
 * lens reconnects on its own), or null when the user must sign in.
 */
async function resolveBootAuth(storage: KeyValueStorage): Promise<Principal | "offline" | null> {
  if (!isServerConfigured()) return null;
  const hadSession = getToken() !== null || (await loadCredentials(storage)) !== null;
  if (getToken() !== null) {
    try {
      return await me();
    } catch (err) {
      if (!(err instanceof ApiError)) return hadSession ? "offline" : null; // network-level failure
      // 401: token expired/revoked.
      if (getSessionKind() === "oidc") {
        // OIDC: there are no cached credentials to replay — silently refresh the
        // access token against the IdP instead. A failed refresh sends the wearer
        // back to the phone to sign in with Authentik again.
        if (await refreshOidcOnBoot()) {
          try {
            return await me();
          } catch {
            /* the refreshed token still didn't confirm — fall through to login */
          }
        }
        return null;
      }
      // built-in: fall through to a silent re-login with the cached credentials.
    }
  }
  const relogged = await silentLogin(storage);
  if (relogged) return relogged;
  return null;
}

/** Prepare the provider from the server advertisement, then refresh the OIDC token. */
async function refreshOidcOnBoot(): Promise<boolean> {
  try {
    const oidc = await advertisedOidc();
    if (!oidc) return false;
    await prepareOidc(oidc);
    await refreshOidcSession();
    return true;
  } catch {
    return false;
  }
}

/**
 * Best-effort: make sure the OIDC provider is prepared for an already-signed-in
 * OIDC session, so a *mid-session* silent refresh (ws.ts / api.ts on a 1008/401)
 * has a token endpoint to hit. A returning user whose stored token is still valid
 * never went through discovery this run, so the glasses' first refresh would
 * otherwise fail for want of a provider.
 */
async function ensureOidcProviderReady(): Promise<void> {
  if (getSessionKind() !== "oidc") return;
  try {
    const oidc = await advertisedOidc();
    if (oidc) await prepareOidc(oidc);
  } catch {
    // Couldn't reach the server to discover the provider. A later refresh then
    // finds no provider and fails closed to re-login (the phone) rather than
    // re-discovering — a rare, self-healing case (the next boot retries this).
  }
}

export async function initPhoneLogin(
  storage: KeyValueStorage,
  els: PhoneLoginElements = queryPhoneLoginElements(),
  callbacks: PhoneLoginCallbacks = {},
  deps: PhoneLoginDeps = {},
): Promise<void> {
  const callbackSearch =
    deps.callbackSearch ?? (typeof window !== "undefined" ? window.location.search : "");
  const clearCallbackUrl =
    deps.clearCallbackUrl ??
    (() => {
      try {
        window.history.replaceState(null, "", window.location.pathname);
      } catch {
        /* no history API (tests / non-browser) — nothing to strip */
      }
    });

  // Prefill the cached choices so a re-login (e.g. after sign-out) is two taps.
  // Shown as the plain host people type (tenir.example.com), never a wss:// URL.
  if (isServerConfigured()) els.server.value = displayServerUrl(config.apiWsUrl);
  const cached = await loadCredentials(storage);
  if (cached) els.user.value = cached.username;

  wireForm(storage, els, callbacks);
  wireOidcButton(els);
  wireSignOut(storage, els, callbacks);
  // Re-probe the OIDC advertisement whenever the wearer changes the server, so
  // the Authentik button appears/disappears to match the entered instance.
  els.server.addEventListener("change", () => {
    void refreshOidcSection(els);
  });

  // Returning from the Authentik redirect: finish the exchange and land in the app.
  if (hasOidcCallback(callbackSearch)) {
    await completeOidcBoot(els, callbacks, callbackSearch, clearCallbackUrl);
    return;
  }

  const authed = await resolveBootAuth(storage);
  if (authed === "offline") {
    // Server unreachable right now, but we have a cached sign-in: show the app
    // anyway (the pages surface their own connection state) rather than
    // demanding a password nobody can check.
    showApp(els, cached?.username ?? "");
    callbacks.onAuthed?.();
    void ensureOidcProviderReady();
  } else if (authed) {
    showApp(els, authed.username);
    callbacks.onAuthed?.();
    void ensureOidcProviderReady();
  } else {
    // Straight to the login form — and tell the lens, so it says "not signed
    // in" rather than implying captions are running.
    showLogin(els);
    callbacks.onSignedOut?.();
    // Offer the Authentik button if the (already-configured) server advertises it.
    await refreshOidcSection(els);
  }
}

/** Point the REST client at the entered server and (re)render the OIDC button. */
async function refreshOidcSection(els: PhoneLoginElements): Promise<void> {
  const wsUrl = normalizeServerUrl(els.server.value);
  if (!wsUrl) {
    renderOidcSection(els, null);
    return;
  }
  // Probe the typed server without persisting it (persist happens on sign-in).
  configureApi({ httpBaseUrl: httpBaseFromWs(wsUrl) });
  renderOidcSection(els, await advertisedOidc());
}

function wireForm(
  storage: KeyValueStorage,
  els: PhoneLoginElements,
  callbacks: PhoneLoginCallbacks,
): void {
  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    void (async () => {
      els.error.classList.remove("show");

      const wsUrl = normalizeServerUrl(els.server.value);
      if (!wsUrl) {
        showError(els, "Enter your server address, e.g. tenir.example.com");
        return;
      }

      els.submit.disabled = true;
      els.submit.textContent = "Logging in…";
      await applyServerUrl(wsUrl); // persists the URL and repoints the REST client
      try {
        const principal = await login(els.user.value.trim(), els.password.value);
        // Cache the credentials (device store) so the token's expiry never asks
        // the user to type them again — the app re-logs-in silently.
        await saveCredentials(storage, {
          username: els.user.value.trim(),
          password: els.password.value,
        });
        els.password.value = "";
        showApp(els, principal.username);
        callbacks.onAuthed?.();
      } catch (err) {
        showError(els, describeLoginError(err));
      } finally {
        els.submit.disabled = false;
        els.submit.textContent = "Log in";
      }
    })();
  });
}

function wireOidcButton(els: PhoneLoginElements): void {
  els.oidcButton.addEventListener("click", () => {
    void (async () => {
      els.error.classList.remove("show");

      const wsUrl = normalizeServerUrl(els.server.value);
      if (!wsUrl) {
        showError(els, "Enter your server address, e.g. tenir.example.com");
        return;
      }

      els.oidcButton.disabled = true;
      els.oidcButton.textContent = "Redirecting…";
      try {
        // Persist the chosen server BEFORE the redirect: this page reloads on the
        // way back, and boot must point client-core at the same instance to
        // complete the exchange.
        await applyServerUrl(wsUrl);
        const oidc = await advertisedOidc();
        if (!oidc) {
          showError(els, "This server doesn't offer Authentik sign-in.");
          return;
        }
        await prepareOidc(oidc); // discovery → provider
        // Navigates to Authentik; the page unloads and boot finishes the callback.
        // (In a non-navigating env this resolves undefined and we simply reset.)
        await startOidcLogin();
      } catch (err) {
        showError(els, describeAuthError(err));
      } finally {
        els.oidcButton.disabled = false;
        els.oidcButton.textContent = "Sign in with Authentik";
      }
    })();
  });
}

/** Finish an OIDC redirect callback on boot: exchange the code and show the app. */
async function completeOidcBoot(
  els: PhoneLoginElements,
  callbacks: PhoneLoginCallbacks,
  search: string,
  clearCallbackUrl: () => void,
): Promise<void> {
  try {
    // The server was persisted before the redirect and applied by initConfig, so
    // re-fetch its advertisement and prepare the provider for the token exchange.
    const oidc = await advertisedOidc();
    if (!oidc) throw new OidcError("this server no longer offers Authentik sign-in");
    await prepareOidc(oidc);
    const principal = await completeOidcCallback(search);
    clearCallbackUrl();
    showApp(els, principal.username);
    callbacks.onAuthed?.();
  } catch (err) {
    // A bad/expired code, CSRF state mismatch, or user-cancelled login: strip the
    // params, drop back to the form with the reason, and re-offer the button.
    clearCallbackUrl();
    showLogin(els);
    showError(els, describeAuthError(err));
    callbacks.onSignedOut?.();
    await refreshOidcSection(els);
  }
}

function wireSignOut(
  storage: KeyValueStorage,
  els: PhoneLoginElements,
  callbacks: PhoneLoginCallbacks,
): void {
  els.signOut.addEventListener("click", () => {
    void (async () => {
      const wasOidc = getSessionKind() === "oidc";
      await clearCredentials(storage);
      showLogin(els);
      callbacks.onSignedOut?.();
      if (wasOidc) {
        // RP-initiated logout (§10): prepare the provider so `oidcLogout` can end
        // the IdP session too, then clear local state. Best-effort — if the server
        // can't be reached, `oidcLogout` still clears the local token + sidecar.
        try {
          const oidc = await advertisedOidc();
          if (oidc) await prepareOidc(oidc);
        } catch {
          /* fall through to a local-only logout */
        }
        await oidcLogout(); // clears token+sidecar; navigates to end_session if advertised
      } else {
        logout(); // clears the bearer token (memory + device store)
      }
    })();
  });
}
