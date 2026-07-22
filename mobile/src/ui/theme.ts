/**
 * Lumen design tokens for the mobile UI — the React Native counterpart of the
 * web SPA's `web/src/styles.css` custom properties. The two palettes carry the
 * exact same hex values as the web tokens (dark is the signature default, light
 * the counterpart); `tests/theme.test.ts` keeps them in lockstep with the CSS.
 *
 * Pure data + helpers (no React) so it unit-tests without a renderer; the
 * theme-switching machinery lives in `ThemeContext.tsx`.
 */

/** One theme's colour tokens (names mirror the web CSS variables). */
export interface Palette {
  /** --bg: the page/screen background. */
  bg: string;
  /** --surface: cards and other first-level surfaces. */
  surface: string;
  /** --surface-raised: one step above a surface (hover fills, neutral chips). */
  surfaceRaised: string;
  /** --border / --border-strong: hairlines and component outlines. */
  border: string;
  borderStrong: string;
  /** --text / --text-muted. */
  text: string;
  muted: string;
  /** --accent / --accent-strong / --accent-ink: the signature teal. */
  accent: string;
  accentStrong: string;
  onAccent: string;
  /** --danger / --danger-ink and the status trio. */
  danger: string;
  onDanger: string;
  success: string;
  warning: string;
}

export const palettes: Record<"dark" | "light", Palette> = {
  // Keep in sync with the `:root` block of web/src/styles.css.
  dark: {
    bg: "#0E1116",
    surface: "#161B22",
    surfaceRaised: "#1C232C",
    border: "#232B36",
    borderStrong: "#313C4A",
    text: "#E6EDF3",
    muted: "#8B97A6",
    accent: "#3FD9C9",
    accentStrong: "#5FE3D5",
    onAccent: "#0E1116",
    danger: "#FF6B6B",
    onDanger: "#0E1116",
    success: "#54E0A6",
    warning: "#E8B341",
  },
  // Keep in sync with the `[data-theme="light"]` block of web/src/styles.css.
  light: {
    bg: "#F7F9FB",
    surface: "#FFFFFF",
    surfaceRaised: "#F0F3F6",
    border: "#E2E8EF",
    borderStrong: "#CBD5DF",
    text: "#0E1116",
    muted: "#5A6675",
    accent: "#0E8C7E",
    accentStrong: "#0B7468",
    onAccent: "#FFFFFF",
    danger: "#C0392B",
    onDanger: "#FFFFFF",
    success: "#0E8C5A",
    warning: "#B8860B",
  },
};

/**
 * A `#RRGGBB` colour at partial opacity — RN's stand-in for the web tokens
 * derived with `color-mix(in srgb, <colour> N%, transparent)` (accent washes,
 * tinted chip borders/fills, danger outlines).
 */
export function withAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** 4px-base spacing scale (mirrors --space-1..6). */
export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 } as const;

/** Radius scale (mirrors --radius-sm/md/lg; pill = fully round badges/tabs). */
export const radius = { sm: 8, md: 11, lg: 14, pill: 999 } as const;

/** The user's theme choice: follow the OS, or pin light/dark (as on web). */
export type ThemeMode = "system" | "light" | "dark";
