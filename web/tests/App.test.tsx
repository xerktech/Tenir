import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import { ToastProvider } from "../src/lib/toast";

const { me } = vi.hoisted(() => ({ me: vi.fn() }));

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
    getState() {
      return {
        running: false,
        connection: "closed" as const,
        listening: false,
        micSource: "phone-microphone" as const,
        segments: [],
        partial: "",
      };
    }
    subscribe(_cb: unknown) { return () => {}; }
    start() { return Promise.resolve(false); }
    stop() { return Promise.resolve(); }
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

  it("shows the household identity and a logout control when authenticated", async () => {
    me.mockResolvedValue({ userId: "u-7f3a", username: "ada", household: "lab", role: "owner" });
    renderApp();
    await waitFor(() => expect(screen.getByText(/ada · lab · owner/)).toBeInTheDocument());
    // The raw user_id is not shown.
    expect(screen.queryByText(/u-7f3a/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
  });

  it("shows the Live tab as the first dashboard section", async () => {
    me.mockResolvedValue({ userId: "u1", username: "ada", household: "h1", role: "owner" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Live" })).toBeInTheDocument());
    // First nav item is Live.
    const nav = screen.getByRole("navigation", { name: "Sections" });
    expect(nav.querySelector("button")?.textContent).toBe("Live");
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
