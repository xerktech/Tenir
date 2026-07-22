/**
 * Tenir web SPA — the self-hosted speech-to-text UI: record live sessions,
 * browse stored transcripts, and watch system health. Built on
 * `@tenir/client-core` against the same REST API.
 *
 * Auth is always required: `me()` resolves the principal when a valid token is
 * stored (straight to the dashboard) and throws 401 otherwise (show the login form).
 */

import { describeLoginError, login, logout, me, type Principal } from "@tenir/client-core";
import { useState, type FormEvent } from "react";

import { useAsync } from "./lib/hooks";
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

export function App(): JSX.Element {
  const { data: principal, loading, reload } = useAsync<Principal | null>(() => me().catch(() => null));

  if (loading) {
    return (
      <main className="container">
        <p className="muted">Connecting…</p>
      </main>
    );
  }

  return (
    <main className="container">
      <Header principal={principal} onAuthChange={reload} />
      {principal ? <Dashboard principal={principal} /> : <Login onLoggedIn={reload} />}
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
  return (
    <header className="app-header">
      <h1 className="wordmark">
        <span className="wordmark-dot" aria-hidden="true" />
        Tenir
      </h1>
      <span className="header-spacer" />
      <ThemeToggle />
      {principal && (
        <>
          <span className="muted">
            {principal.username} · {principal.household} · {principal.role}
          </span>
          <Button
            variant="ghost"
            onClick={() => {
              logout();
              onAuthChange();
            }}
          >
            Log out
          </Button>
        </>
      )}
    </header>
  );
}

function Login({ onLoggedIn }: { onLoggedIn: () => void }): JSX.Element {
  const notify = useNotify();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    login(username, password)
      .then(() => {
        notify("Logged in");
        onLoggedIn();
      })
      .catch((err) => notify(describeLoginError(err), "err"));
  };

  return (
    <section>
      <h2>Log in</h2>
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
      </form>
      <p className="muted">Log in to the household on your self-hosted instance.</p>
    </section>
  );
}

function Dashboard({ principal }: { principal: Principal }): JSX.Element {
  const isAdmin = principal.role === "admin";
  const tabs: Tab[] = [...BASE_TABS, ...(isAdmin ? ADMIN_TABS : [])];
  const [tab, setTab] = useState<Tab>("Live");
  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Sections">
        {tabs.map((t) => (
          <button
            key={t}
            className={`nav-item ${t === tab ? "active" : ""}`.trim()}
            aria-current={t === tab ? "page" : undefined}
            onClick={() => setTab(t)}
          >
            <NavIcon page={t} />
            <span className="nav-label">{t}</span>
          </button>
        ))}
      </nav>
      <div className="content">
        {tab === "Live" && <LivePanel />}
        {tab === "History" && <HistoryPanel />}
        {tab === "Status" && <StatusPanel />}
        {tab === "Users" && isAdmin && <UsersPanel me={principal} />}
      </div>
    </div>
  );
}
