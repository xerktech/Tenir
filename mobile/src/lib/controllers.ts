/**
 * Container hooks (master plan §8.2): all the screen *behaviour* — fetching, search,
 * mutations — written once on `@tenir/client-core`, with the React Native screens
 * left as thin presenters over the state these return. Because they import only `react`
 * and `client-core` (never `react-native`), they are unit-tested under vitest, exactly
 * like the web SPA's panels.
 */

import {
  getStatus,
  history,
  login,
  logout,
  me,
  NetworkError,
  type Conversation,
  type ConversationSummary,
  type Principal,
  type SystemStatus,
} from "@tenir/client-core";
import { useCallback, useEffect, useState } from "react";

import { useAsync, type AsyncState } from "./useAsync";

// ---- auth -------------------------------------------------------------------

export interface AuthController extends AsyncState<Principal | null> {
  signIn(username: string, password: string): Promise<void>;
  signOut(): void;
}

export function useAuth(): AuthController {
  // null = not logged in yet (the api 401s /auth/me without a valid token).
  const state = useAsync<Principal | null>(() => me().catch(() => null));
  const signIn = useCallback(
    async (username: string, password: string) => {
      await login(username, password);
      state.reload();
    },
    [state],
  );
  const signOut = useCallback(() => {
    logout();
    state.reload();
  }, [state]);
  return { ...state, signIn, signOut };
}

// ---- history ----------------------------------------------------------------

export interface HistoryController extends AsyncState<ConversationSummary[]> {
  search: string;
  setSearch(q: string): void;
  open(id: string): Promise<Conversation>;
  remove(id: string): Promise<void>;
}

export function useHistory(): HistoryController {
  const [search, setSearch] = useState("");
  const state = useAsync(() => history.list(search.trim() || undefined), [search]);
  const remove = useCallback(
    async (id: string) => {
      await history.remove(id);
      state.reload();
    },
    [state],
  );
  return { ...state, search, setSearch, open: (id) => history.get(id), remove };
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
