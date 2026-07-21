import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { applyTheme, cycleTheme, getTheme, initTheme, setTheme, type Theme } from "../src/theme";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

describe("theme", () => {
  it("defaults to system when nothing stored", () => {
    expect(getTheme()).toBe("system");
  });

  it("reads a stored theme", () => {
    localStorage.setItem("tenir.theme", "dark");
    expect(getTheme()).toBe("dark");
  });

  it("applyTheme sets data-theme for explicit themes and clears it for system", () => {
    applyTheme("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    applyTheme("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("setTheme persists and applies", () => {
    setTheme("dark");
    expect(localStorage.getItem("tenir.theme")).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("cycleTheme rotates system -> light -> dark -> system", () => {
    const order: Theme[] = ["system", "light", "dark"];
    expect(order.map(cycleTheme)).toEqual(["light", "dark", "system"]);
  });

  it("initTheme applies the stored theme", () => {
    localStorage.setItem("tenir.theme", "light");
    initTheme();
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });
});
