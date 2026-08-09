import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StatusPanel } from "../src/panels/Status";

const { getStatus, NetworkError, ApiError } = vi.hoisted(() => ({
  getStatus: vi.fn(),
  NetworkError: class NetworkError extends Error {},
  // The panel now renders the failure reason via errText(), which needs ApiError.
  ApiError: class ApiError extends Error {
    status = 500;
  },
}));

vi.mock("@tenir/client-core", () => ({
  NetworkError,
  ApiError,
  getStatus: () => getStatus(),
}));

describe("StatusPanel", () => {
  it("lists each component with a state label", async () => {
    getStatus.mockResolvedValue({
      overall: "degraded",
      generatedAt: "2026-06-19T00:00:00+00:00",
      components: [
        { id: "stt", label: "Live STT (Parakeet)", category: "model", state: "connecting", detail: "loading", checkedAt: "x" },
        { id: "llm", label: "Cue LLM", category: "model", state: "ready", detail: "reachable via LiteLLM gateway", checkedAt: "x" },
        { id: "postgres", label: "Database (Postgres)", category: "infra", state: "ready", detail: "reachable", checkedAt: "x" },
      ],
    });
    render(<StatusPanel />);
    await waitFor(() => expect(screen.getByText("Live STT (Parakeet)")).toBeInTheDocument());
    expect(screen.getByText("Cue LLM")).toBeInTheDocument();
    expect(screen.getByText("Some components degraded")).toBeInTheDocument();
    expect(screen.getByText("Connecting…")).toBeInTheDocument();
    expect(screen.getByText("Database (Postgres)")).toBeInTheDocument();
    // The connecting component renders a yellow light.
    expect(document.querySelector(".status-dot--connecting")).not.toBeNull();
  });

  it("shows the system as down when the server is unreachable", async () => {
    getStatus.mockRejectedValue(new NetworkError("could not reach the server"));
    render(<StatusPanel />);
    await waitFor(() => expect(screen.getByText(/Can't reach the server/)).toBeInTheDocument());
    expect(document.querySelector(".status-dot--down")).not.toBeNull();
  });

  it("notes when no components are configured", async () => {
    getStatus.mockResolvedValue({ overall: "ready", generatedAt: "x", components: [] });
    render(<StatusPanel />);
    await waitFor(() =>
      expect(screen.getByText(/No components are configured/)).toBeInTheDocument(),
    );
  });

  it("reports a non-network API failure instead of claiming nothing is monitored (XERK-236)", async () => {
    // /status returning 500 is the ONE case a status page exists for. The catch
    // only set `unreachable` for NetworkError, so any other error left `status`
    // null and the panel fell through to the "all good, nothing configured"
    // copy — reassurance, on the screen that reports trouble.
    getStatus.mockRejectedValue(new Error("500: boom"));
    render(<StatusPanel />);
    await waitFor(() =>
      expect(screen.getByText(/Could not read system status/)).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/No components are configured to monitor/),
    ).not.toBeInTheDocument();
  });

  it("still shows the server-unreachable banner for a NetworkError", async () => {
    getStatus.mockRejectedValue(new NetworkError("down"));
    render(<StatusPanel />);
    await waitFor(() => expect(screen.getByText(/Can.t reach the server/)).toBeInTheDocument());
  });
});
