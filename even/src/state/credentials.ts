/**
 * Cached sign-in for the Even Hub app (XERK-82).
 *
 * The api's bearer tokens expire (24h TTL by default), so caching only the token
 * would still mean re-typing the password every day. Like Turma's glasses app, we
 * persist the username + password in the device store (bridge-backed on real
 * glasses) and re-login silently whenever the token is missing or rejected — the
 * user signs in once and the glasses keep working. Sign-out clears everything.
 */

import { login, type Principal } from "@tenir/client-core";

import type { KeyValueStorage } from "./storage";

export const CREDENTIALS_KEY = "tenir.credentials";

export interface Credentials {
  username: string;
  password: string;
}

/** The cached credentials, or null when the user has never signed in (or signed out). */
export async function loadCredentials(storage: KeyValueStorage): Promise<Credentials | null> {
  const raw = await storage.get(CREDENTIALS_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<Credentials>;
    if (typeof parsed.username !== "string" || typeof parsed.password !== "string") return null;
    return { username: parsed.username, password: parsed.password };
  } catch {
    return null;
  }
}

export async function saveCredentials(storage: KeyValueStorage, creds: Credentials): Promise<void> {
  await storage.set(CREDENTIALS_KEY, JSON.stringify(creds));
}

export async function clearCredentials(storage: KeyValueStorage): Promise<void> {
  await storage.remove(CREDENTIALS_KEY);
}

/**
 * Re-login with the cached credentials (e.g. after the stored token expired).
 * Returns the principal on success, or null when there are no cached credentials
 * or the server rejects them — callers fall back to the phone login page.
 * A fresh token lands in the client-core token store as a `login()` side effect.
 */
export async function silentLogin(storage: KeyValueStorage): Promise<Principal | null> {
  const creds = await loadCredentials(storage);
  if (!creds) return null;
  try {
    return await login(creds.username, creds.password);
  } catch {
    return null;
  }
}
