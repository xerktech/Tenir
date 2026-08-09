/**
 * System status — a red/yellow/green light per backend component (master plan §status).
 *
 * Polls the api's GET /status snapshot so an operator can confirm every component
 * (infra stores, the model servers, the LiteLLM gateway) is healthy — and see a
 * model that's still loading as yellow rather than a silent failure. A transport
 * failure means the api itself is unreachable, rendered as the whole system down.
 */

import { getStatus, NetworkError, type ComponentState, type SystemStatus } from "@tenir/client-core";
import { useEffect, useState } from "react";

import { errText } from "../lib/toast";
import { Spinner, StatusLight } from "../ui";

const POLL_MS = 4000;

const STATE_LABEL: Record<ComponentState, string> = {
  ready: "Ready",
  connecting: "Connecting…",
  down: "Down",
};

const OVERALL_TEXT: Record<SystemStatus["overall"], string> = {
  ready: "All systems ready",
  degraded: "Some components degraded",
  down: "System down",
};

function overallLight(overall: SystemStatus["overall"]): ComponentState {
  if (overall === "ready") return "ready";
  if (overall === "down") return "down";
  return "connecting";
}

export function StatusPanel(): JSX.Element {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const next = await getStatus();
        if (!alive) return;
        setStatus(next);
        setUnreachable(false);
        setFailure(null);
      } catch (err) {
        if (!alive) return;
        // A NetworkError means the api itself is unreachable — show the system down.
        setUnreachable(err instanceof NetworkError);
        // Anything else (a 500 from /status, a 401, a malformed body) used to
        // leave `status` null, so the panel fell through to "no components are
        // configured to monitor" — reassurance, on the one screen that exists
        // to report that something is wrong (XERK-236). Keep the reason.
        setFailure(err instanceof NetworkError ? null : errText(err));
      } finally {
        if (alive) setLoaded(true);
      }
    };
    void tick();
    const id = setInterval(() => void tick(), POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!loaded) {
    return (
      <section>
        <h2>System status</h2>
        <Spinner />
      </section>
    );
  }

  if (unreachable) {
    return (
      <section>
        <h2>System status</h2>
        <div className="status-banner">
          <StatusLight state="down" />
          <span>Can&apos;t reach the server — it may be down, or the server URL may be wrong.</span>
        </div>
      </section>
    );
  }

  if (failure != null && status == null) {
    return (
      <section>
        <h2>System status</h2>
        <div className="status-banner">
          <StatusLight state="down" />
          <span>Could not read system status — {failure}</span>
        </div>
      </section>
    );
  }

  const components = status?.components ?? [];
  return (
    <section>
      <h2>System status</h2>
      {status && (
        <div className="status-summary">
          <StatusLight state={overallLight(status.overall)} />
          <span>{OVERALL_TEXT[status.overall]}</span>
        </div>
      )}
      {components.length === 0 ? (
        <p className="muted">No components are configured to monitor on this deployment.</p>
      ) : (
        <ul className="status-list">
          {components.map((c) => (
            <li className="status-row" key={c.id}>
              <StatusLight state={c.state} />
              <span className="status-name">{c.label}</span>
              <span className="status-state muted">{STATE_LABEL[c.state]}</span>
              <span className="status-detail muted">{c.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
