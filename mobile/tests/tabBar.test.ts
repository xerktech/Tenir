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

  it("lists exactly the five dashboard tabs", () => {
    const decl = app.match(/const TABS = \[([^\]]*)\]/)?.[1] ?? "";
    for (const t of TABS) expect(decl).toContain(`"${t}"`);
  });
});

// XERK-80 parity: the web SPA keeps its tab across a page refresh (URL hash);
// the mobile equivalent is restoring the last tab across an app relaunch.
describe("last-tab restore across relaunches", () => {
  const app = readText("src/App.tsx");
  const bootstrap = readText("src/bootstrap.ts");

  it("boots into the persisted tab instead of hardcoding Live", () => {
    expect(bootstrap).toContain("loadLastTab");
    expect(app).toContain("useState<Tab>(initialTab)");
    // Unknown/absent persisted values still land on Live.
    expect(app).toContain('function asTab(');
  });

  it("persists each tab switch", () => {
    expect(app).toContain("saveLastTab(deviceKeyValue(), next)");
    expect(app).toContain("<TabBar tab={tab} onSelect={selectTab} />");
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
