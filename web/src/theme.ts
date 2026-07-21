/** Theme state for the Tenir web UI: persists a choice and reflects it on <html>. */

export type Theme = "system" | "light" | "dark";

const KEY = "tenir.theme";
const ORDER: Theme[] = ["system", "light", "dark"];

export function getTheme(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" || v === "system" ? v : "system";
  } catch {
    return "system";
  }
}

/** Reflect the theme on <html>: explicit themes set data-theme, system clears it. */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function setTheme(theme: Theme): void {
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* storage unavailable — just won't persist */
  }
  applyTheme(theme);
}

export function cycleTheme(current: Theme): Theme {
  return ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
}

export function initTheme(): void {
  applyTheme(getTheme());
}
