import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";

import * as core from "@tenir/client-core";
import { App, OIDC_CALLBACK_PATH } from "../src/App";
import { ToastProvider } from "../src/lib/toast";

// The mocked OIDC surface (see vi.mock below), pulled back typed for per-test tweaks.
const getAuthConfig = core.getAuthConfig as unknown as Mock;
const getSessionKind = core.getSessionKind as unknown as Mock;
const oidcReady = core.oidcReady as unknown as Mock;
const prepareOidc = core.prepareOidc as unknown as Mock;
const startOidcLogin = core.startOidcLogin as unknown as Mock;
const completeOidcCallback = core.completeOidcCallback as unknown as Mock;
const oidcLogout = core.oidcLogout as unknown as Mock;
const logout = core.logout as unknown as Mock;

const { me, captureStats } = vi.hoisted(() => ({
  me: vi.fn(),
  // Instrumentation for the capture session so a test can prove the session is
  // created once, above the tab switch, and never stopped on navigation, and can
  // drive it into a "running" state to assert the background affordance (XERK-111).
  captureStats: { constructed: 0, stops: 0, running: false },
}));

vi.mock("@tenir/client-core", () => ({
  configureApi: vi.fn(),
  // config.ts (pulled in transitively) wires the browser OIDC primitives at import.
  configureOidc: vi.fn(),
  browserOidcPrimitives: vi.fn((redirectUri: string) => ({ redirectUri })),
  me,
  login: vi.fn(),
  logout: vi.fn(),
  describeLoginError: (err: unknown) => (err instanceof Error ? err.message : String(err)),
  // OIDC surface (docs/auth-oidc.md §10). Default: server advertises OIDC off, so
  // the existing suites see the login form + dashboard exactly as before.
  getAuthConfig: vi.fn(async () => ({ builtin: true })),
  getSessionKind: vi.fn(() => "builtin"),
  oidcReady: vi.fn(() => false),
  prepareOidc: vi.fn(async () => {}),
  startOidcLogin: vi.fn(async () => {}),
  completeOidcCallback: vi.fn(),
  oidcLogout: vi.fn(async () => {}),
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
        activeCueEndsAt: null,
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

// XERK-654: the optional "Sign in with Authentik" path. The username/password
// form is always present; the OIDC button is gated on the server advertising it,
// and the redirect callback / sign-out are handled for both session kinds.
describe("OIDC login (docs/auth-oidc.md §10)", () => {
  const OIDC_ON = {
    builtin: true,
    oidc: {
      enabled: true,
      issuer: "https://idp.example/application/o/tenir/",
      clientId: "tenir-web",
      scopes: ["openid", "email", "profile", "groups"],
    },
  };

  afterEach(() => {
    // Reset the OIDC surface to its "off / built-in" defaults so overrides here
    // never leak into the suites above.
    getAuthConfig.mockReset();
    getAuthConfig.mockResolvedValue({ builtin: true });
    getSessionKind.mockReset();
    getSessionKind.mockReturnValue("builtin");
    oidcReady.mockReset();
    oidcReady.mockReturnValue(false);
    for (const m of [prepareOidc, startOidcLogin, completeOidcCallback, oidcLogout, logout]) {
      m.mockReset();
    }
    prepareOidc.mockResolvedValue(undefined);
    startOidcLogin.mockResolvedValue(undefined);
    oidcLogout.mockResolvedValue(undefined);
  });

  it("shows only the username/password form when the server has OIDC off", async () => {
    getAuthConfig.mockResolvedValue({ builtin: true });
    me.mockRejectedValue(new Error("401"));
    renderApp();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument());
    // The form is always present...
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log in" })).toBeInTheDocument();
    // ...and with OIDC off, no Authentik button.
    expect(screen.queryByRole("button", { name: /Authentik/ })).not.toBeInTheDocument();
  });

  it("adds the Authentik button beside the form when the server advertises OIDC", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    me.mockRejectedValue(new Error("401"));
    renderApp();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Sign in with Authentik" })).toBeInTheDocument(),
    );
    // The built-in form is still there in addition to the OIDC button.
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log in" })).toBeInTheDocument();
  });

  it("prepares the provider and starts the redirect when the button is clicked", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    oidcReady.mockReturnValue(false); // not yet discovered
    me.mockRejectedValue(new Error("401"));
    renderApp();
    const btn = await screen.findByRole("button", { name: "Sign in with Authentik" });
    fireEvent.click(btn);
    await waitFor(() => expect(prepareOidc).toHaveBeenCalledWith(OIDC_ON.oidc));
    expect(startOidcLogin).toHaveBeenCalledTimes(1);
  });

  it("skips discovery when the provider is already resolved", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    oidcReady.mockReturnValue(true); // provider already known
    me.mockRejectedValue(new Error("401"));
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "Sign in with Authentik" }));
    await waitFor(() => expect(startOidcLogin).toHaveBeenCalledTimes(1));
    expect(prepareOidc).not.toHaveBeenCalled();
  });

  it("surfaces an error when starting the redirect fails, and stays on the login screen", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    prepareOidc.mockRejectedValue(new Error("OIDC discovery failed (503)"));
    me.mockRejectedValue(new Error("401"));
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "Sign in with Authentik" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("OIDC discovery failed (503)"));
    expect(startOidcLogin).not.toHaveBeenCalled();
    // Still the login screen, with the form intact.
    expect(screen.getByRole("button", { name: "Log in" })).toBeInTheDocument();
  });

  it("completes the redirect callback and lands signed in, clearing the URL", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    completeOidcCallback.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "member" });
    me.mockClear(); // this suite shares the hoisted `me`; assert on this test's calls only
    // Boot on the redirect path the IdP returns to.
    window.history.replaceState(null, "", `${OIDC_CALLBACK_PATH}?code=the-code&state=the-state`);
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "History" })).toBeInTheDocument());
    // The provider was resolved and the code exchanged from the query string.
    expect(prepareOidc).toHaveBeenCalledWith(OIDC_ON.oidc);
    expect(completeOidcCallback).toHaveBeenCalledWith("?code=the-code&state=the-state");
    // me() is never needed on the callback path — the exchange returns the principal.
    expect(me).not.toHaveBeenCalled();
    // The callback path is scrubbed from the address bar so tab routing resumes.
    expect(window.location.pathname).toBe("/");
  });

  it("shows the failure on the login screen when the callback exchange rejects", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    completeOidcCallback.mockRejectedValue(new Error("OIDC state mismatch — possible CSRF, login rejected"));
    window.history.replaceState(null, "", `${OIDC_CALLBACK_PATH}?code=x&state=bad`);
    renderApp();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent("OIDC state mismatch");
    // Even on failure the callback path is cleared.
    expect(window.location.pathname).toBe("/");
  });

  it("does not treat a bare post-logout return (no code) as a callback", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    me.mockClear();
    me.mockRejectedValue(new Error("401"));
    window.history.replaceState(null, "", OIDC_CALLBACK_PATH); // no ?code / ?error
    renderApp();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument());
    expect(completeOidcCallback).not.toHaveBeenCalled();
    // Falls through to me() and the path is cleaned so login isn't stuck on it.
    expect(me).toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
  });

  it("resolves the provider on boot for an existing OIDC session (so silent refresh works)", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    getSessionKind.mockReturnValue("oidc"); // a stored OIDC session
    me.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "member" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "History" })).toBeInTheDocument());
    expect(prepareOidc).toHaveBeenCalledWith(OIDC_ON.oidc);
  });

  it("does not prepare the provider on boot for a built-in session", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    getSessionKind.mockReturnValue("builtin");
    me.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "admin" });
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "History" })).toBeInTheDocument());
    expect(prepareOidc).not.toHaveBeenCalled();
  });

  it("signs out an OIDC session via RP-initiated logout, not the local-only clear", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON);
    getSessionKind.mockReturnValue("oidc");
    me.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "member" });
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "Log out" }));
    await waitFor(() => expect(oidcLogout).toHaveBeenCalledTimes(1));
    expect(logout).not.toHaveBeenCalled();
  });

  it("signs out a built-in session with the local token clear", async () => {
    getAuthConfig.mockResolvedValue(OIDC_ON); // OIDC available, but this session is built-in
    getSessionKind.mockReturnValue("builtin");
    me.mockResolvedValue({ userId: "u", username: "ada", household: "lab", role: "member" });
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "Log out" }));
    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
    expect(oidcLogout).not.toHaveBeenCalled();
  });

  it("falls back to the form alone when /auth/config is unreachable", async () => {
    getAuthConfig.mockRejectedValue(new Error("404"));
    me.mockRejectedValue(new Error("401"));
    renderApp();
    await waitFor(() => expect(screen.getByRole("button", { name: "Log in" })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Authentik/ })).not.toBeInTheDocument();
  });
});
