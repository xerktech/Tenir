/**
 * Phone-side bottom navigation (XERK-93): Session ⇄ History.
 *
 * The web UI's phone navigation pattern (styles.css mobile layout) as plain
 * DOM: a fixed bottom bar of icon tabs that swaps which page is visible.
 * Everything happens inside the one WebView — navigating away would unload the
 * lens app, so tabs only toggle `hidden` on the page containers.
 */

export type PhonePage = "session" | "history";

const PAGES: readonly PhonePage[] = ["session", "history"];

export interface PhoneNavElements {
  session: { tab: HTMLButtonElement; page: HTMLElement };
  history: { tab: HTMLButtonElement; page: HTMLElement };
}

/**
 * The nav's elements, or null when the page doesn't carry them (tests that
 * mount only another slice) — the caller then skips the nav rather than
 * failing the whole app.
 */
export function queryPhoneNavElements(doc: Document = document): PhoneNavElements | null {
  const sessionTab = doc.getElementById("nav-session");
  const sessionPage = doc.getElementById("page-session");
  const historyTab = doc.getElementById("nav-history");
  const historyPage = doc.getElementById("page-history");
  if (!sessionTab || !sessionPage || !historyTab || !historyPage) return null;
  return {
    session: { tab: sessionTab as HTMLButtonElement, page: sessionPage },
    history: { tab: historyTab as HTMLButtonElement, page: historyPage },
  };
}

export interface PhoneNav {
  /** Bring a page to the front (also what tab clicks call). Fires `onShow`. */
  show(page: PhonePage): void;
  current(): PhonePage;
}

/**
 * Wire the tabs. `onShow` fires on every `show()` — including a repeat of the
 * current page — so a page can refresh itself whenever it is (re)activated.
 */
export function initPhoneNav(
  els: PhoneNavElements,
  onShow?: (page: PhonePage) => void,
): PhoneNav {
  let current: PhonePage = "session";

  const apply = () => {
    for (const page of PAGES) {
      const active = page === current;
      els[page].page.hidden = !active;
      els[page].tab.classList.toggle("active", active);
      if (active) els[page].tab.setAttribute("aria-current", "page");
      else els[page].tab.removeAttribute("aria-current");
    }
  };

  const show = (page: PhonePage) => {
    current = page;
    apply();
    onShow?.(page);
  };

  els.session.tab.addEventListener("click", () => show("session"));
  els.history.tab.addEventListener("click", () => show("history"));
  apply(); // assert the initial state without firing onShow

  return { show, current: () => current };
}
