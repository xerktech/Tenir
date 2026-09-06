/**
 * Tenir web SPA — the self-hosted speech-to-text UI: record live sessions,
 * browse stored transcripts, and watch system health. Built on
 * `@tenir/client-core` against the same REST API.
 *
 * Auth is always required: `me()` resolves the principal when a valid token is
 * stored (straight to the dashboard) and throws 401 otherwise (show the login form).
 */

import {
  ApiError,
  completeOidcCallback,
  describeLoginError,
  getAuthConfig,
  getSessionKind,
  login,
  logout,
  me,
  NetworkError,
  oidcLogout,
  oidcReady,
  prepareOidc,
  type Principal,
  type ServerAuthConfig,
  startOidcLogin,
} from "@tenir/client-core";
import { useState, type FormEvent } from "react";

import { CaptureProvider, useCaptureContext } from "./lib/capture";
import { useAsync } from "./lib/hooks";
import { useHashTab } from "./lib/route";
import { useNotify } from "./lib/toast";
import { HistoryPanel } from "./panels/History";
import { LivePanel } from "./panels/Live";
import { StatusPanel } from "./panels/Status";
import { UsersPanel } from "./panels/Users";
import { Button, Field, Input, NavIcon, ThemeToggle } from "./ui";

const BASE_TABS = ["Live", "History", "Status"] as const;
// User management is an admin-only surface (the server 403s members), so the
// tab is only offered to admins.
const ADMIN_TABS = ["Users"] as const;
type Tab = (typeof BASE_TABS)[number] | (typeof ADMIN_TABS)[number];

// The same-origin OIDC redirect the SPA lands on after Authentik authorizes the
// user (docs/auth-oidc.md §10). The api serves the SPA at this path so the app
// boots here and completes the exchange from the query string.
export const OIDC_CALLBACK_PATH = "/auth/oidc/callback";

/** On the OIDC redirect path at all — including a bare post-logout return. */
function onOidcCallbackPath(loc: Location = window.location): boolean {
  return loc.pathname.endsWith(OIDC_CALLBACK_PATH);
}

/** An actual authorization callback: the redirect path carrying a code or error. */
function isOidcCallbackRoute(loc: Location = window.location): boolean {
  return onOidcCallbackPath(loc) && /[?&](code|error)=/.test(loc.search);
}

/** Drop the callback path from the address bar so tab (hash) routing resumes. */
function clearOidcCallbackUrl(): void {
  window.history.replaceState(null, "", "/");
}

/** Friendly text for an OIDC failure; defers to `describeLoginError` for transport. */
function describeOidcError(err: unknown): string {
  if (err instanceof NetworkError || err instanceof ApiError) return describeLoginError(err);
  if (err instanceof Error) return err.message;
  return String(err);
}

/** The advertised OIDC config, or null when the server has OIDC off/unavailable. */
type OidcConfig = NonNullable<ServerAuthConfig["oidc"]>;
function enabledOidc(config: ServerAuthConfig | null): OidcConfig | null {
  return config?.oidc?.enabled ? config.oidc : null;
}

interface BootState {
  principal: Principal | null;
  authConfig: ServerAuthConfig | null;
  /** A failed OIDC callback exchange, surfaced on the login screen. */
  oidcError: string | null;
}

/**
 * Resolve the initial auth state. Fetches the server's auth advertisement (to
 * gate the OIDC button and, for an OIDC session, to resolve the provider so
 * silent refresh works), then either completes an in-flight OIDC redirect or
 * confirms an existing token via `me()`. Every OIDC step degrades gracefully: a
 * server with OIDC off (or an unreachable `/auth/config`) behaves exactly as
 * before — the username/password form and nothing else.
 */
async function boot(): Promise<BootState> {
  const authConfig = await getAuthConfig().catch(() => null);
  const oidc = enabledOidc(authConfig);

  if (isOidcCallbackRoute()) {
    try {
      if (!oidc) throw new Error("The server is no longer offering Authentik sign-in.");
      await prepareOidc(oidc);
      const principal = await completeOidcCallback(window.location.search);
      return { principal, authConfig, oidcError: null };
    } catch (err) {
      return { principal: null, authConfig, oidcError: describeOidcError(err) };
    } finally {
      clearOidcCallbackUrl();
    }
  }

  // An existing OIDC session must have its provider resolved so the transports'
  // 401 silent-refresh (api.ts/ws.ts) can reach the IdP token endpoint.
  if (oidc && getSessionKind() === "oidc") {
    await prepareOidc(oidc).catch(() => {});
  }
  // A bare return from RP-initiated logout lands on the callback path with no
  // code — clear it so the login screen isn't stuck on that URL.
  if (onOidcCallbackPath()) clearOidcCallbackUrl();

  const principal = await me().catch(() => null);
  return { principal, authConfig, oidcError: null };
}

export function App(): JSX.Element {
  const { data, loading, reload } = useAsync<BootState>(boot);

  if (loading || !data) {
    return (
      <main className="container">
        <p className="muted">Connecting…</p>
      </main>
    );
  }

  return (
    <main className="container">
      <Header principal={data.principal} onAuthChange={reload} />
      {data.principal ? (
        <Dashboard principal={data.principal} />
      ) : (
        <Login oidc={enabledOidc(data.authConfig)} initialError={data.oidcError} onLoggedIn={reload} />
      )}
    </main>
  );
}

function Header({
  principal,
  onAuthChange,
}: {
  principal: Principal | null;
  onAuthChange: () => void;
}): JSX.Element {
  // Sign-out covers both session kinds: an OIDC session additionally ends the
  // IdP session (RP-initiated logout, which navigates away and returns here),
  // while a built-in session is a purely local token clear (docs/auth-oidc.md §10).
  const signOut = () => {
    void (async () => {
      if (getSessionKind() === "oidc") await oidcLogout();
      else logout();
      onAuthChange();
    })();
  };
  return (
    <header className="app-header">
      <h1 className="wordmark">
        <span className="wordmark-dot" aria-hidden="true" />
        Tenir
      </h1>
      <span className="header-spacer" />
      <ThemeToggle />
      {principal && (
        <Button variant="ghost" onClick={signOut}>
          Log out
        </Button>
      )}
    </header>
  );
}

function Login({
  oidc,
  initialError,
  onLoggedIn,
}: {
  oidc: OidcConfig | null;
  initialError: string | null;
  onLoggedIn: () => void;
}): JSX.Element {
  const notify = useNotify();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  // A failed OIDC callback lands back on the login screen; show why once.
  const [error, setError] = useState<string | null>(initialError);
  const [oidcBusy, setOidcBusy] = useState(false);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    login(username, password)
      .then(() => {
        notify("Logged in");
        onLoggedIn();
      })
      .catch((err) => notify(describeLoginError(err), "err"));
  };

  // Start the redirect to Authentik. `prepareOidc` resolves the provider from
  // discovery the first time; `startOidcLogin` then navigates the page away, so
  // this promise never resolves on success (the page unloads).
  const signInWithAuthentik = () => {
    if (!oidc) return;
    setError(null);
    setOidcBusy(true);
    void (async () => {
      try {
        if (!oidcReady()) await prepareOidc(oidc);
        await startOidcLogin();
      } catch (err) {
        setError(describeOidcError(err));
        setOidcBusy(false);
      }
    })();
  };

  return (
    <section>
      <h2>Log in</h2>
      {error && (
        <p className="field-error" role="alert" style={{ maxWidth: "20rem" }}>
          {error}
        </p>
      )}
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)", maxWidth: "20rem" }}>
        <Field label="Username" htmlFor="login-user">
          <Input id="login-user" placeholder="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label="Password" htmlFor="login-pass">
          <Input
            id="login-pass"
            placeholder="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Button variant="primary" type="submit">
          Log in
        </Button>
        {/* Shown only when the server advertises OIDC (docs/auth-oidc.md §10);
            with OIDC off the login card is byte-for-byte the previous one. */}
        {oidc && (
          <>
            <div className="auth-or" aria-hidden="true">
              <span>or</span>
            </div>
            <Button variant="secondary" type="button" onClick={signInWithAuthentik} disabled={oidcBusy}>
              {oidcBusy ? "Redirecting…" : "Sign in with Authentik"}
            </Button>
          </>
        )}
      </form>
      <p className="muted">Log in to the household on your self-hosted instance.</p>
    </section>
  );
}

function Dashboard({ principal }: { principal: Principal }): JSX.Element {
  // The capture session lives above the tab switch so a live recording keeps
  // running when you move to another tab (XERK-111); the shell reads it back to
  // signal the ongoing recording from anywhere in the dashboard.
  return (
    <CaptureProvider>
      <DashboardShell principal={principal} />
    </CaptureProvider>
  );
}

function DashboardShell({ principal }: { principal: Principal }): JSX.Element {
  const isAdmin = principal.role === "admin";
  const tabs: Tab[] = [...BASE_TABS, ...(isAdmin ? ADMIN_TABS : [])];
  // The active tab is mirrored into the URL hash so a page refresh (or a
  // shared link) restores the same tab instead of resetting to Live (XERK-80).
  const [tab, setTab] = useHashTab<Tab>(tabs, "Live");
  const { controller } = useCaptureContext();
  const recording = controller.state.running;
  return (
    <div className="shell">
      <nav className="nav-tabs" aria-label="Sections">
        {tabs.map((t) => (
          <button
            key={t}
            className={`nav-item ${t === tab ? "active" : ""}`.trim()}
            aria-current={t === tab ? "page" : undefined}
            onClick={() => setTab(t)}
          >
            <span className="nav-icon-wrap">
              <NavIcon page={t} />
              {/* A live recording keeps running in the background (XERK-111): a
                  pulsing dot on the Live item signals it from every tab. */}
              {t === "Live" && recording && <span className="rec-dot" aria-hidden="true" />}
            </span>
            <span className="nav-label">{t}</span>
          </button>
        ))}
      </nav>
      <div className="content">
        {/* Away from Live while recording: a reassurance the session is still
            live, and a one-tap way back to it (XERK-111). */}
        {recording && tab !== "Live" && (
          <button type="button" className="bg-recording" onClick={() => setTab("Live")}>
            <span className="rec-dot" aria-hidden="true" />
            Recording in the background — return to Live
          </button>
        )}
        {tab === "Live" && <LivePanel />}
        {tab === "History" && <HistoryPanel />}
        {tab === "Status" && <StatusPanel />}
        {tab === "Users" && isAdmin && <UsersPanel me={principal} />}
      </div>
    </div>
  );
}
