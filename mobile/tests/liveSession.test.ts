import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Source-level checks (the RN components aren't rendered in this jsdom suite,
// mirroring tabBar.test.ts). They guard the XERK-111 wiring: the capture session
// is hoisted above the tab switch so a live recording keeps running in the
// background when you leave the Live tab, and the ongoing state is surfaced from
// every screen.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("live session persistence across tabs (XERK-111)", () => {
  const app = readText("src/App.tsx");
  const live = readText("src/screens/Live.tsx");
  const capture = readText("src/lib/capture.tsx");

  it("hoists the capture session above the tab switch via CaptureProvider", () => {
    // The provider wraps the persistent dashboard shell that owns the tab state,
    // so switching tabs no longer unmounts the session.
    expect(app).toContain("<CaptureProvider wsUrl={wsUrl}>");
    expect(app).toContain("<DashboardShell");
    // The provider is what creates the single session (via useCapture).
    expect(capture).toContain("useCapture(wsUrl, cueLevel)");
  });

  it("renders the Live screen without re-creating its own session", () => {
    // The Live screen no longer takes a wsUrl / calls useCapture — it reads the
    // shared session from context, so tab switches can't tear it down.
    expect(app).toContain("{tab === \"Live\" && <LiveScreen />}");
    expect(live).toContain("useCaptureContext()");
    expect(live).not.toContain("useCapture(");
  });

  it("keeps the session tied to the dashboard, not the Live tab, in useCapture", () => {
    // The only teardown remains the screen/URL-change cleanup; now that the
    // provider is the mount point, that fires on sign-out / server change only.
    const useCapture = readText("src/lib/useCapture.ts");
    expect(useCapture).toContain("void session.stop();");
  });
});

describe("background-recording affordance (XERK-111)", () => {
  const app = readText("src/App.tsx");

  it("marks the Live tab with a recording dot while running", () => {
    expect(app).toContain("t === \"Live\" && recording");
    expect(app).toContain("styles.tabRecDot");
  });

  it("shows a return-to-Live bar on other tabs while recording", () => {
    expect(app).toContain("recording && tab !== \"Live\" && <BackgroundRecordingBar");
    expect(app).toContain("onReturn={() => selectTab(\"Live\")}");
    // The bar is an accessible button that jumps back to the Live screen.
    expect(app).toContain('accessibilityRole="button"');
  });
});
