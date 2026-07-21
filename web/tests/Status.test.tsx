import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StatusPanel } from "../src/panels/Status";

const { getStatus, NetworkError } = vi.hoisted(() => ({
  getStatus: vi.fn(),
  NetworkError: class NetworkError extends Error {},
}));

vi.mock("@tenir/client-core", () => ({
  NetworkError,
  getStatus: () => getStatus(),
}));

describe("StatusPanel", () => {
  it("lists each component with a state label", async () => {
    getStatus.mockResolvedValue({
      overall: "degraded",
      generatedAt: "2026-06-19T00:00:00+00:00",
      components: [
        { id: "stt", label: "Live STT (Voxtral)", category: "model", state: "connecting", detail: "loading", checkedAt: "x" },
        { id: "postgres", label: "Database (Postgres)", category: "infra", state: "ready", detail: "reachable", checkedAt: "x" },
      ],
    });
    render(<StatusPanel />);
    await waitFor(() => expect(screen.getByText("Live STT (Voxtral)")).toBeInTheDocument());
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
});
