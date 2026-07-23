/**
 * Phone-side bottom navigation (XERK-93): Session ⇄ History tab switching —
 * pure DOM toggles inside the one WebView (navigating away would unload the
 * lens app).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { initPhoneNav, queryPhoneNavElements } from "../src/phone/nav";

function mountDom(): void {
  document.body.innerHTML = `
    <div id="page-session"></div>
    <div id="page-history" hidden></div>
    <nav>
      <button id="nav-session" class="nav-item active" aria-current="page"></button>
      <button id="nav-history" class="nav-item"></button>
    </nav>
  `;
}

beforeEach(mountDom);
afterEach(() => {
  document.body.innerHTML = "";
});

const tab = (id: string) => document.getElementById(id)! as HTMLButtonElement;
const page = (id: string) => document.getElementById(id)!;

describe("queryPhoneNavElements", () => {
  it("finds the tabs and pages", () => {
    expect(queryPhoneNavElements()).not.toBeNull();
  });

  it("returns null when the page has no nav (instead of failing the app)", () => {
    document.body.innerHTML = "";
    expect(queryPhoneNavElements()).toBeNull();
  });
});

describe("initPhoneNav", () => {
  it("starts on Session without firing onShow", () => {
    const onShow = vi.fn();
    const nav = initPhoneNav(queryPhoneNavElements()!, onShow);
    expect(nav.current()).toBe("session");
    expect(page("page-session").hidden).toBe(false);
    expect(page("page-history").hidden).toBe(true);
    expect(onShow).not.toHaveBeenCalled();
  });

  it("switches pages, tab state and aria on tab clicks", () => {
    const onShow = vi.fn();
    const nav = initPhoneNav(queryPhoneNavElements()!, onShow);

    tab("nav-history").click();
    expect(nav.current()).toBe("history");
    expect(page("page-session").hidden).toBe(true);
    expect(page("page-history").hidden).toBe(false);
    expect(tab("nav-history").classList.contains("active")).toBe(true);
    expect(tab("nav-history").getAttribute("aria-current")).toBe("page");
    expect(tab("nav-session").classList.contains("active")).toBe(false);
    expect(tab("nav-session").hasAttribute("aria-current")).toBe(false);
    expect(onShow).toHaveBeenCalledWith("history");

    tab("nav-session").click();
    expect(nav.current()).toBe("session");
    expect(page("page-session").hidden).toBe(false);
    expect(onShow).toHaveBeenCalledWith("session");
  });

  it("fires onShow on a repeat of the current page (so History can refresh)", () => {
    const onShow = vi.fn();
    initPhoneNav(queryPhoneNavElements()!, onShow);
    tab("nav-history").click();
    tab("nav-history").click();
    expect(onShow).toHaveBeenCalledTimes(2);
  });

  it("show() drives the same switch programmatically", () => {
    const onShow = vi.fn();
    const nav = initPhoneNav(queryPhoneNavElements()!, onShow);
    nav.show("history");
    expect(page("page-history").hidden).toBe(false);
    nav.show("session");
    expect(page("page-session").hidden).toBe(false);
    expect(onShow).toHaveBeenCalledTimes(2);
  });
});
