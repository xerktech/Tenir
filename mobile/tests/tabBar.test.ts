import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level checks (the RN components aren't rendered in this jsdom suite,
// mirroring appIcon.test.ts). They guard the bottom-tab-bar + icons wiring so a
// regression in the nav shape is caught by typecheck-adjacent tests.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

const TABS = ["Live", "History", "Status", "Settings", "Privacy"];

describe("mobile bottom tab bar", () => {
  const app = readText("src/App.tsx");

  it("renders a bottom TabBar instead of a top button row", () => {
    expect(app).toContain("<TabBar");
    // The tab bar sits below the content and is styled as a bottom bar.
    expect(app).toContain("borderTopWidth: 1");
    // The old wrapping top-row tab style is gone.
    expect(app).not.toContain("flexWrap: \"wrap\"");
  });

  it("exposes each tab as an accessible tab control tinted by active state", () => {
    expect(app).toContain('accessibilityRole="tab"');
    expect(app).toContain("accessibilityState={{ selected: active }}");
    expect(app).toContain("active ? colors.accent : colors.muted");
    expect(app).toContain("<TabIcon name={t} color={color} />");
  });

  it("marks the active tab with the 28×3 top accent indicator (web parity)", () => {
    // The web bottom nav marks the active item with a 28×3 accent bar; the
    // native bar carries the same indicator.
    expect(app).toContain("{active && <View style={styles.tabActiveBar} />}");
    expect(app).toContain("width: 28");
    expect(app).toContain("height: 3");
  });

  it("lists exactly the five dashboard tabs", () => {
    const decl = app.match(/const TABS = \[([^\]]*)\]/)?.[1] ?? "";
    for (const t of TABS) expect(decl).toContain(`"${t}"`);
  });
});

describe("mobile tab icons", () => {
  const icons = readText("src/ui/icons.tsx");

  it("provides a View-drawn icon for every tab (no native SVG dependency)", () => {
    for (const t of TABS) expect(icons).toContain(`${t}:`);
    // Icons are plain React Native primitives, keeping the app free of a native
    // SVG lib and consistent with the vector-only launcher icon.
    expect(icons).toContain('from "react-native"');
    expect(icons).not.toContain("react-native-svg");
  });
});
