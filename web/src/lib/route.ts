/**
 * Hash-based tab routing (XERK-80): the active dashboard tab lives in the URL
 * fragment (`#/history`), so refreshing the page — or opening a shared link —
 * lands on the same tab instead of resetting to Live, and the browser
 * back/forward buttons walk tab history.
 *
 * The fragment (rather than the path) is used deliberately: the api serves the
 * built SPA via `StaticFiles(html=True)`, which has no catch-all SPA fallback,
 * so a path like `/history` would 404 on refresh. The fragment never reaches
 * the server, so it works unchanged under both `vite dev` and the container.
 */

import { useEffect, useRef, useState } from "react";

/** The URL fragment naming a tab: `"History"` → `"#/history"`. */
export function tabToHash(tab: string): string {
  return `#/${tab.toLowerCase()}`;
}

/**
 * Resolve a URL fragment back to one of the offered tabs. Unknown slugs — and
 * tabs the current user isn't offered (e.g. `#/users` for a non-admin) — fall
 * back to the default tab rather than rendering nothing.
 */
export function tabFromHash<T extends string>(hash: string, tabs: readonly T[], fallback: T): T {
  const slug = hash.replace(/^#\/?/, "").toLowerCase();
  return tabs.find((t) => t.toLowerCase() === slug) ?? fallback;
}

/**
 * `useState` for the active tab, mirrored into `location.hash`. The initial
 * value comes from the current URL (so a refresh restores the tab), selecting
 * a tab writes the fragment (creating a history entry), and external hash
 * changes — back/forward, manual edits — update the state.
 */
export function useHashTab<T extends string>(tabs: readonly T[], fallback: T): [T, (t: T) => void] {
  // The offered tab set can change across renders (e.g. admin state resolving);
  // keep the latest in a ref so the one hashchange listener always validates
  // against the current set without re-subscribing.
  const tabsRef = useRef(tabs);
  tabsRef.current = tabs;

  const [tab, setTab] = useState<T>(() => tabFromHash(window.location.hash, tabs, fallback));

  useEffect(() => {
    const onHashChange = () => setTab(tabFromHash(window.location.hash, tabsRef.current, fallback));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [fallback]);

  const select = (next: T) => {
    // Update state directly for an instant switch; the hash write also fires
    // hashchange, which re-derives the same value (a no-op re-render).
    setTab(next);
    window.location.hash = tabToHash(next);
  };

  return [tab, select];
}
