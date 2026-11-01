/**
 * Container hooks (master plan §8.2): all the screen *behaviour* — fetching, search,
 * mutations — written once on `@tenir/client-core`, with the React Native screens
 * left as thin presenters over the state these return. Because they import only `react`
 * and `client-core` (never `react-native`), they are unit-tested under vitest, exactly
 * like the web SPA's panels.
 */

import {
  checkSession,
  getSessionKind,
  getStatus,
  history,
  login,
  logout,
  NetworkError,
  oidcLogout,
  startOidcLogin,
  type Conversation,
  type ConversationSummary,
  type OrgView,
  type Principal,
  type SystemStatus,
} from "@tenir/client-core";
import { useCallback, useEffect, useState } from "react";

import { useAsync, type AsyncState } from "./useAsync";

// ---- auth -------------------------------------------------------------------

export interface AuthController extends AsyncState<Principal | null> {
  signIn(username: string, password: string): Promise<void>;
  /**
   * Complete the native OIDC login (XERK-655) and re-check identity. The server's
   * provider must already be resolved (`probeOidcAvailable` on the setup screen).
   */
  signInWithOidc(): Promise<void>;
  signOut(): void;
}

export function useAuth(): AuthController {
  // null = not logged in yet (the api 401s /auth/me without a valid token).
  //
  // Only an AUTH failure means that. Swallowing every error here collapsed a
  // NetworkError into "not logged in", so a self-hosted server being briefly
  // unreachable dropped the user onto the empty "Set up Tenir" screen with no
  // hint that the server was down — their token was fine all along, and
  // relaunching once the server was back signed them straight in. It reads as
  // the app forgetting the login, and trains people to retype their password
  // (XERK-236). `checkSession` (shared with web) lets a server fault — unreachable
  // or any 5xx — surface as an error with the token kept, and drops a dead token on
  // a 401 so a later outage can't claim the user is still signed in (XERK-1591).
  const state = useAsync<Principal | null>(checkSession);
  const signIn = useCallback(
    async (username: string, password: string) => {
      await login(username, password);
      state.reload();
    },
    [state],
  );
  const signInWithOidc = useCallback(async () => {
    // `startOidcLogin` drives the browser round-trip and, on the native primitive,
    // completes the code exchange inline (token stored, identity confirmed) before it
    // resolves; reload re-runs `me()` to land on the dashboard.
    await startOidcLogin();
    state.reload();
  }, [state]);
  const signOut = useCallback(() => {
    // An OIDC session additionally ends the IdP session (RP-initiated logout, which
    // opens the browser); both kinds clear the local token first. `oidcLogout` clears
    // the token synchronously before its redirect, so reloading right away lands on the
    // login screen either way.
    if (getSessionKind() === "oidc") void oidcLogout();
    else logout();
    state.reload();
  }, [state]);
  return { ...state, signIn, signInWithOidc, signOut };
}

// ---- history ----------------------------------------------------------------

export interface HistoryController extends AsyncState<ConversationSummary[]> {
  search: string;
  setSearch(q: string): void;
  /** Org site key the list is filtered to; "" = every org (XERK-2507). */
  org: string;
  setOrg(siteKey: string): void;
  /** Orgs the caller has meetings from; empty (filter hidden) when none or unloadable. */
  orgs: OrgView[];
  open(id: string): Promise<Conversation>;
  remove(id: string): Promise<void>;
}

export function useHistory(): HistoryController {
  const [search, setSearch] = useState("");
  const [org, setOrg] = useState("");
  const state = useAsync(
    () => history.list(search.trim() || undefined, 50, 0, org || undefined),
    [search, org],
  );
  // A failed org list just hides the filter: the history itself still loads.
  const orgs = useAsync(() => history.orgs().catch((): OrgView[] => []), []);
  const remove = useCallback(
    async (id: string) => {
      await history.remove(id);
      state.reload();
    },
    [state],
  );
  return {
    ...state,
    search,
    setSearch,
    org,
    setOrg,
    orgs: orgs.data ?? [],
    open: (id) => history.get(id),
    remove,
  };
}

// ---- component status dashboard ---------------------------------------------

export interface StatusController {
  status: SystemStatus | null;
  /** The api itself is unreachable (transport failure) — render the system down. */
  unreachable: boolean;
  loaded: boolean;
}

export function useStatus(pollMs = 4000): StatusController {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const next = await getStatus();
        if (!alive) return;
        setStatus(next);
        setUnreachable(false);
      } catch (err) {
        if (!alive) return;
        setUnreachable(err instanceof NetworkError);
      } finally {
        if (alive) setLoaded(true);
      }
    };
    void tick();
    const id = setInterval(() => void tick(), pollMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [pollMs]);

  return { status, unreachable, loaded };
}
