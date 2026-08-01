import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level checks (the hook pulls in react-native, which doesn't load under
// this jsdom suite — mirroring updateBanner.test.ts). They guard XERK-167: the
// updater keeps checking for the life of the app — on foreground and on a timer
// — instead of only once at launch. The pure throttle/state-fold logic these
// triggers run through is unit-tested in updater.test.ts.
const src = readFileSync(resolve(process.cwd(), "src/lib/useUpdater.ts")).toString("utf8");

describe("useUpdater re-check wiring (XERK-167)", () => {
  it("re-checks when the app returns to the foreground", () => {
    expect(src).toContain('AppState.addEventListener("change"');
    expect(src).toContain('status === "active"');
  });

  it("re-checks periodically while the app stays open, and only in the foreground", () => {
    expect(src).toContain("setInterval");
    expect(src).toContain("CHECK_THROTTLE_MS");
    expect(src).toContain('AppState.currentState === "active"');
  });

  it("throttles every trigger through the shared shouldCheck gate", () => {
    expect(src).toContain("shouldCheck(lastCheckAt.current");
    expect(src).toContain("checking.current");
  });

  it("folds re-check results through the pure applyCheckResult transition", () => {
    expect(src).toContain("applyCheckResult(prev, update, dismissed.current)");
  });

  it("tears the listeners down on unmount", () => {
    expect(src).toContain("sub.remove()");
    expect(src).toContain("clearInterval(timer)");
  });
});
