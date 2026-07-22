/**
 * Theme state for the mobile app — the RN counterpart of the web SPA's
 * `theme.ts` + `ThemeToggle`. The mode (system/light/dark) is persisted under
 * the same key the web uses (`tenir.theme`) via the injected key/value store;
 * "system" follows the OS appearance, and dark stays the signature default when
 * the OS reports nothing.
 */

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useColorScheme } from "react-native";

import { loadThemeMode, saveThemeMode, type KeyValueStore } from "../storage";
import { palettes, type Palette, type ThemeMode } from "./theme";

export interface ThemeValue {
  /** The active palette (resolved from mode + OS appearance). */
  colors: Palette;
  /** The resolved scheme the palette was picked for. */
  scheme: "dark" | "light";
  /** The user's persisted choice. */
  mode: ThemeMode;
  /** Change + persist the choice. */
  setMode: (mode: ThemeMode) => void;
}

// Dark-palette default so plain component tests render without a provider.
const ThemeContext = createContext<ThemeValue>({
  colors: palettes.dark,
  scheme: "dark",
  mode: "system",
  setMode: () => undefined,
});

export function ThemeProvider({
  kv,
  children,
}: {
  /** Backing store for the persisted mode; omit to keep it in memory only. */
  kv?: KeyValueStore;
  children: ReactNode;
}): JSX.Element {
  const system = useColorScheme();
  const [mode, setModeState] = useState<ThemeMode>("system");

  useEffect(() => {
    if (!kv) return;
    void loadThemeMode(kv).then((stored) => {
      if (stored) setModeState(stored);
    });
  }, [kv]);

  const value = useMemo<ThemeValue>(() => {
    const scheme = mode === "system" ? (system === "light" ? "light" : "dark") : mode;
    return {
      colors: palettes[scheme],
      scheme,
      mode,
      setMode: (next: ThemeMode) => {
        setModeState(next);
        if (kv) void saveThemeMode(kv, next);
      },
    };
  }, [mode, system, kv]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeValue {
  return useContext(ThemeContext);
}

/**
 * Memoised themed styles: pass a module-level `makeStyles(colors)` factory
 * (stable identity, so the memo only re-runs when the palette flips).
 */
export function useThemedStyles<T>(makeStyles: (colors: Palette) => T): T {
  const { colors } = useTheme();
  return useMemo(() => makeStyles(colors), [makeStyles, colors]);
}
