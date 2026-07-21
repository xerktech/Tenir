import { useState } from "react";

import { cycleTheme, getTheme, setTheme, type Theme } from "../theme";

const LABEL: Record<Theme, string> = { system: "System", light: "Light", dark: "Dark" };

/** Header control that cycles system -> light -> dark and persists the choice. */
export function ThemeToggle(): JSX.Element {
  const [theme, setThemeState] = useState<Theme>(() => getTheme());

  const advance = () => {
    const next = cycleTheme(theme);
    setTheme(next);
    setThemeState(next);
  };

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={advance}
      aria-label={`Theme: ${LABEL[theme]}. Click to change.`}
    >
      {LABEL[theme]}
    </button>
  );
}
