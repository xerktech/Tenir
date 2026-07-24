import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import { ToastProvider } from "../src/lib/toast";

const { me, captureStats } = vi.hoisted(() => ({
  me: vi.fn(),
  // Instrumentation for the capture session so a test can prove the session is
  // created once, above the tab switch, and never stopped on navigation, and can
  // drive it into a "running" state to assert the background affordance (XERK-111).
  captureStats: { constructed: 0, stops: 0, running: false },
}));

vi.mock("@tenir/client-core", () => ({
  configureApi: vi.fn(),
  me,
  login: vi.fn(),
  logout: vi.fn(),
  ApiError: class ApiError extends Error {},
  NetworkError: class NetworkError extends Error {},
  getStatus: vi.fn(async () => ({ overall: "ready", generatedAt: "x", reasons: [], components: [] })),
  history: { list: vi.fn(async () => []), get: vi.fn(), remove: vi.fn(), audioUrl: () => "" },
  users: { list: vi.fn(async () => []), create: vi.fn(), remove: vi.fn() },
  // Needed by LivePanel / useCapture
  DISCLOSURES: [{ id: "recording", title: "Recording notice", body: "Test body." }],
  wsFromHttpBase: vi.fn((url: string) => url.replace(/^http/, "ws")),
  ApiClient: class ApiClient {
    constructor(_url: string, _handlers: object) {}
    start() {}
    stop() {}
    send() {}
    sendAudio() { return false; }
    get currentSessionId() { return null; }
  },
  browserAudioSource: vi.fn(() => ({
    requestPermission: vi.fn(async () => {}),
    start: vi.fn(async () => {}),
    stop: vi.fn(async () => {}),
  })),
  CaptureSession: class CaptureSession {
    constructor() { captureStats.constructed += 1; }
    getState() {
      return {
        running: captureStats.running,
        connection: "closed" as const,
        listening: false,
        micSource: "phone-microphone" as const,
        segments: [],
        partial: "",
        activeCue: null,
        queuedCues: [],
        pastCues: [],
      };
    }
    subscribe(_cb: unknown) { return () => {}; }
    start() { return Promise.resolve(false); }
    stop() { captureStats.stops += 1; return Promise.resolve(); }
    togglePause() {}
  },
}));

function renderApp() {
  return render(
    <ToastProvider>
      <App />
    </ToastProvider>,
  );
}

// Tab navigation writes the URL fragment (XERK-80); drop it after every test so
// each one starts from the default (Live) tab. replaceState avoids firing a
// stray hashchange at unmounted listeners.
afterEach(() => {
  window.history.replaceState(null, "", window.location.pathname);
});

// Each test sets `me`'s behaviour outright (resolve/reject), so no reset is needed
// between them — and `mockReset()` here would make vitest flag the deliberate 401
// rejection as unhandled.
describe("App auth gating", () => {
  it("shows the login form when the api rejects /auth/me (JWT auth on)", async () => {
    // A 401 from /auth/me means we're not logged in (auth is always required).
    me.mockImplementation(() => Promise.reject(new Error("401")));
    renderApp();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "History" })).not.toBeInTheDocument();
  });

  it("renders the full dashboard once authenticated", async () => {
    me.mockResolvedValue({ userId: "u", username: "ada", household: "h", role: "owner" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "History" })).toBeInTheDocument());
    // Every surface is reachable from the tab bar.
    for (const tab of ["Live", "History", "Status"]) {
      expect(screen.getByRole("button", { name: tab })).toBeInTheDocument();
    }
    // Removed surfaces are gone from the nav.
    for (const tab of ["Chat", "Digest", "Speakers", "People", "Documents", "Feeds", "Policy", "Integrations", "Settings"]) {
      expect(screen.queryByRole("button", { name: tab })).not.toBeInTheDocument();
    }
    // Auth is always required, so a logged-in user always has a logout control.
    expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
    // The api URL is a deploy-time docker var now, not an in-app field.
    expect(screen.queryByLabelText("Server")).not.toBeInTheDocument();
  });

  it("offers the admin-only Users tab to admins and reveals the panel", async () => {
    me.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "admin" });
    renderApp();
    const usersTab = await screen.findByRole("button", { name: "Users" });
    fireEvent.click(usersTab);
    await waitFor(() =>
      expect(screen.getByText(/Add or remove members of the lab household/)).toBeInTheDocument(),
    );
  });

  it("hides the Users tab from non-admins", async () => {
    me.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "member" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Users" })).not.toBeInTheDocument();
  });

  it("shows a logout control but no user identity in the header when authenticated", async () => {
    me.mockResolvedValue({ userId: "u-7f3a", username: "ada", household: "lab", role: "owner" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument());
    // The header no longer surfaces user info (username/household/role or raw user_id).
    expect(screen.queryByText(/ada · lab · owner/)).not.toBeInTheDocument();
    expect(screen.queryByText(/u-7f3a/)).not.toBeInTheDocument();
  });

  it("shows the Live tab as the first dashboard section", async () => {
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "owner" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument());
    // First nav item is Live.
    const nav = screen.getByRole("navigation", { name: "Sections" });
    expect(nav.querySelector("button")?.textContent).toBe("Live");
  });

  it("gives every nav item a decorative icon without changing its accessible name", async () => {
    me.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "admin" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument());

    const nav = screen.getByRole("navigation", { name: "Sections" });
    // Each page button renders exactly one icon glyph...
    for (const tab of ["Live", "History", "Status", "Users"]) {
      const button = screen.getByRole("button", { name: tab });
      expect(button.querySelectorAll("svg")).toHaveLength(1);
      // ...and the icon is decorative, so the label still names the button.
      expect(button.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    }
    // Every nav button has an icon (one svg per page button in the sidebar).
    expect(nav.querySelectorAll("button svg")).toHaveLength(4);
  });

  it("marks only the active nav item with aria-current and moves it on tab change", async () => {
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "owner" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument());

    // Live is active on load — assistive tech announces it as the current page.
    expect(screen.getByRole("button", { name: "Live" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "History" })).not.toHaveAttribute("aria-current");

    // Switching tabs moves the marker so exactly one item is ever current.
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "History" })).toHaveAttribute("aria-current", "page"),
    );
    expect(screen.getByRole("button", { name: "Live" })).not.toHaveAttribute("aria-current");
    expect(
      screen.getAllByRole("button").filter((b) => b.getAttribute("aria-current") === "page"),
    ).toHaveLength(1);
  });
});

// XERK-80: the active tab lives in the URL hash, so refreshing any page keeps
// the user on that page instead of resetting to Live.
describe("URL hash routing", () => {
  it("restores the tab named in the URL on load — a refresh keeps the page", async () => {
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "owner" });
    // Simulate reloading while on History: the fragment survives the refresh.
    window.history.replaceState(null, "", "#/history");
    renderApp();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "History" })).toHaveAttribute("aria-current", "page"),
    );
    expect(screen.getByRole("button", { name: "Live" })).not.toHaveAttribute("aria-current");
  });

  it("writes the selected tab into the URL when navigating", async () => {
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "owner" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Status" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Status" }));
    await waitFor(() => expect(window.location.hash).toBe("#/status"));
  });

  it("follows browser back/forward via hashchange", async () => {
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "owner" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument());
    // pushState doesn't fire hashchange itself, so dispatch it as the browser
    // would on back/forward.
    window.history.pushState(null, "", "#/status");
    fireEvent(window, new Event("hashchange"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Status" })).toHaveAttribute("aria-current", "page"),
    );
  });

  it("falls back to Live for an unknown fragment", async () => {
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "owner" });
    window.history.replaceState(null, "", "#/no-such-page");
    renderApp();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Live" })).toHaveAttribute("aria-current", "page"),
    );
  });

  it("keeps the capture session alive across tab switches (XERK-111)", async () => {
    captureStats.constructed = 0;
    captureStats.stops = 0;
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "owner" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument());

    // One session is created for the dashboard, above the tabs...
    expect(captureStats.constructed).toBe(1);

    // ...and moving Live -> History -> Status -> Live neither tears it down nor
    // spins up a new one: the recording keeps running in the background.
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "History" })).toHaveAttribute("aria-current", "page"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Status" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Status" })).toHaveAttribute("aria-current", "page"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Live" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Live" })).toHaveAttribute("aria-current", "page"),
    );

    expect(captureStats.constructed).toBe(1);
    expect(captureStats.stops).toBe(0);
  });

  it("surfaces a background-recording affordance on other tabs while recording (XERK-111)", async () => {
    captureStats.running = true;
    try {
      me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "owner" });
      renderApp();
      await waitFor(() => expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument());

      // On Live itself there's no "return to Live" prompt — you're already there.
      expect(screen.queryByText(/Recording in the background/)).not.toBeInTheDocument();

      // Move to History: a prompt appears that the recording is still live, and
      // tapping it jumps back to the Live panel.
      fireEvent.click(screen.getByRole("button", { name: "History" }));
      const banner = await screen.findByRole("button", { name: /Recording in the background/ });
      fireEvent.click(banner);
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Live" })).toHaveAttribute("aria-current", "page"),
      );
      expect(screen.queryByText(/Recording in the background/)).not.toBeInTheDocument();
    } finally {
      captureStats.running = false;
    }
  });

  it("keeps non-admins deep-linking #/users on Live", async () => {
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "member" });
    window.history.replaceState(null, "", "#/users");
    renderApp();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Live" })).toHaveAttribute("aria-current", "page"),
    );
    expect(screen.queryByRole("button", { name: "Users" })).not.toBeInTheDocument();
  });

  it("lands admins deep-linking #/users on the Users panel", async () => {
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "lab", role: "admin" });
    window.history.replaceState(null, "", "#/users");
    renderApp();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Users" })).toHaveAttribute("aria-current", "page"),
    );
  });
});
