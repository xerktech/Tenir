import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level checks (the RN components aren't rendered in this jsdom suite,
// mirroring tabBar.test.ts). They guard the XERK-91 restyle: the update banner
// is a Turma-style card below the header that slides in from the side, not the
// old full-width strip above it.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("update banner placement (XERK-91)", () => {
  const app = readText("src/App.tsx");

  it("mounts below the header, not above it", () => {
    const header = app.indexOf("styles.header");
    const banner = app.indexOf("<UpdateBanner />");
    expect(header).toBeGreaterThan(-1);
    expect(banner).toBeGreaterThan(header);
  });
});

describe("update banner card (Turma UpdateBanner.kt parity)", () => {
  const src = readText("src/ui/UpdateBanner.tsx");

  it("is a rounded accent-tinted card, not an edge-to-edge strip", () => {
    // Turma's 14dp-corner card, in Tenir's tint formula (accent wash + border).
    expect(src).toContain("borderRadius: radius.lg");
    expect(src).toContain("withAlpha(colors.accent");
    expect(src).toContain("marginHorizontal: space.sm");
    // The old strip's accent underline is gone.
    expect(src).not.toContain("borderBottomWidth");
  });

  it("slides in from the side on the native driver", () => {
    expect(src).toContain("translateX");
    expect(src).toContain("Animated.timing");
    expect(src).toContain("useNativeDriver: true");
    // The card also animates out: dismissal waits for the exit run to finish.
    expect(src).toContain("finished && setShown(null)");
  });

  it("carries Turma's leading system-update icon, drawn without SVG", () => {
    expect(src).toContain("UpdateIcon");
    expect(src).not.toContain("react-native-svg");
  });

  it("mirrors Turma's copy per state", () => {
    expect(src).toContain("Update available — v");
    expect(src).toContain("Downloading v");
    expect(src).toContain("Ready to install — v");
    expect(src).toContain('"Update failed"');
    expect(src).toContain('"Update"');
    expect(src).toContain('"Install"');
    expect(src).toContain('"Retry"');
  });

  it("shows inline download progress and a spinner while downloading", () => {
    expect(src).toContain('shown.kind === "downloading" && shown.pct !== null');
    expect(src).toContain("ActivityIndicator");
  });

  it("offers Later only while the update is still just an offer", () => {
    // As in Turma: a downloaded/ready update can't be waved away — the only
    // action left is to finish installing it.
    expect(src).toContain('shown.kind === "available" && (');
    expect(src).toContain(">Later</Text>");
    expect(src).not.toContain('"Dismiss"');
  });
});
