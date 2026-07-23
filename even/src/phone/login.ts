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
 * embeds the server-hosted Tenir web UI full-bleed: the phone companion IS the
 * web UI. The token rides the iframe URL as a `#token=` fragment, which the web
 * app adopts at boot, so the embedded UI is already signed in.
 *
 * Everything is driven through injected elements/callbacks so it unit-tests
 * under jsdom without the Even SDK.
 */

import {
  ApiError,
  describeLoginError,
  displayServerUrl,
  getToken,
  login,
  logout,
  me,
  normalizeServerUrl,
  type Principal,
} from "@tenir/client-core";

import { applyServerUrl, config, isServerConfigured } from "../config";
import { clearCredentials, loadCredentials, saveCredentials, silentLogin } from "../state/credentials";
import type { KeyValueStorage } from "../state/storage";

export interface PhoneLoginElements {
  login: HTMLElement; // the login card wrapper
  app: HTMLElement; // the signed-in view
  dashboard: HTMLIFrameElement; // the embedded Tenir web UI
  form: HTMLFormElement;
  server: HTMLInputElement;
  user: HTMLInputElement;
  password: HTMLInputElement;
  submit: HTMLButtonElement;
  error: HTMLElement;
  signOut: HTMLButtonElement;
  appUser: HTMLElement;
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
    dashboard: byId<HTMLIFrameElement>("dashboard"),
    form: byId<HTMLFormElement>("login-form"),
    server: byId<HTMLInputElement>("server-url"),
    user: byId<HTMLInputElement>("username"),
    password: byId<HTMLInputElement>("password"),
    submit: byId<HTMLButtonElement>("login-submit"),
    error: byId("login-error"),
    signOut: byId<HTMLButtonElement>("sign-out"),
    appUser: byId("app-user"),
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
 * The URL the embedded web UI loads: the server's own origin, with the current
 * bearer token in the fragment so the web app boots signed in (it adopts the
 * token and strips the fragment; a fragment never reaches the server or logs).
 */
export function dashboardUrl(httpBaseUrl: string, token: string | null): string {
  const base = `${httpBaseUrl.replace(/\/$/, "")}/`;
  return token ? `${base}#token=${encodeURIComponent(token)}` : base;
}

function showError(els: PhoneLoginElements, msg: string): void {
  els.error.textContent = msg;
  els.error.classList.add("show");
}

function showDashboard(els: PhoneLoginElements, username: string): void {
  els.appUser.textContent = username;
  els.dashboard.src = dashboardUrl(config.apiHttpUrl, getToken());
  els.login.hidden = true;
  els.app.hidden = false;
}

function showLogin(els: PhoneLoginElements): void {
  // Leaving the dashboard: blank the iframe so a signed-out view isn't left
  // holding an authenticated web UI.
  els.dashboard.src = "about:blank";
  els.login.hidden = false;
  els.app.hidden = true;
}

/**
 * Resolve the boot state: with a configured server, try the cached token
 * (`me()`), then a silent re-login with the cached credentials. Returns the
 * principal when signed in, "offline" when the server can't be reached but a
 * cached sign-in exists (show the dashboard best-effort — the lens reconnects on
 * its own), or null when the user must sign in.
 */
async function resolveBootAuth(storage: KeyValueStorage): Promise<Principal | "offline" | null> {
  if (!isServerConfigured()) return null;
  const hadSession = getToken() !== null || (await loadCredentials(storage)) !== null;
  if (getToken() !== null) {
    try {
      return await me();
    } catch (err) {
      if (!(err instanceof ApiError)) return hadSession ? "offline" : null; // network-level failure
      // 401: token expired/revoked — fall through to a silent re-login.
    }
  }
  const relogged = await silentLogin(storage);
  if (relogged) return relogged;
  return null;
}

export async function initPhoneLogin(
  storage: KeyValueStorage,
  els: PhoneLoginElements = queryPhoneLoginElements(),
  callbacks: PhoneLoginCallbacks = {},
): Promise<void> {
  // Prefill the cached choices so a re-login (e.g. after sign-out) is two taps.
  // Shown as the plain host people type (tenir.example.com), never a wss:// URL.
  if (isServerConfigured()) els.server.value = displayServerUrl(config.apiWsUrl);
  const cached = await loadCredentials(storage);
  if (cached) els.user.value = cached.username;

  const authed = await resolveBootAuth(storage);
  if (authed === "offline") {
    // Server unreachable right now, but we have a cached sign-in: show the
    // embedded web UI anyway (it surfaces its own connection state) rather than
    // demanding a password nobody can check.
    showDashboard(els, cached?.username ?? "");
    callbacks.onAuthed?.();
  } else if (authed) {
    showDashboard(els, authed.username);
    callbacks.onAuthed?.();
  } else {
    // Straight to the login form — and tell the lens, so it says "not signed
    // in" rather than implying captions are running.
    showLogin(els);
    callbacks.onSignedOut?.();
  }

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
        showDashboard(els, principal.username);
        callbacks.onAuthed?.();
      } catch (err) {
        showError(els, describeLoginError(err));
      } finally {
        els.submit.disabled = false;
        els.submit.textContent = "Log in";
      }
    })();
  });

  els.signOut.addEventListener("click", () => {
    void (async () => {
      logout(); // clears the bearer token (memory + device store)
      await clearCredentials(storage);
      showLogin(els);
      callbacks.onSignedOut?.();
    })();
  });
}
