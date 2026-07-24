import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CaptureController } from "../src/lib/useCapture";
import { LivePanel, LiveView } from "../src/panels/Live";

const baseState = () => ({
  running: false,
  connection: "closed" as const,
  listening: true,
  micSource: "phone-microphone" as const,
  segments: [] as { id: string; text: string }[],
  partial: "",
  activeCue: null as { id: string; title: string; body: string } | null,
  queuedCues: [] as { id: string; title: string; body: string }[],
});

function fakeController(overrides: Partial<CaptureController["state"]> = {}): CaptureController {
  return {
    state: { ...baseState(), ...overrides },
    start: vi.fn().mockResolvedValue(true),
    stop: vi.fn().mockResolvedValue(undefined),
    togglePause: vi.fn(),
  };
}

function renderLive(controller: CaptureController, onCueLevelChange = vi.fn()) {
  return {
    onCueLevelChange,
    ...render(
      <LiveView controller={controller} cueLevel="balanced" onCueLevelChange={onCueLevelChange} />,
    ),
  };
}

describe("LiveView", () => {
  it("renders final turns and the live partial", () => {
    renderLive(
      fakeController({
        running: true,
        connection: "open",
        segments: [{ id: "a", text: "hello world" }],
        partial: "and th",
      }),
    );
    expect(screen.getByText(/hello world/)).toBeInTheDocument();
    expect(screen.getByText(/and th/)).toBeInTheDocument();
    expect(screen.getByText("open")).toBeInTheDocument();
  });

  it("shows Record when idle and calls start", () => {
    const c = fakeController();
    renderLive(c);
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    expect(c.start).toHaveBeenCalled();
  });

  it("shows Pause/Stop while running and toggles pause", () => {
    const c = fakeController({ running: true, connection: "open" });
    renderLive(c);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(c.togglePause).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("renders the active cue card above the transcript", () => {
    renderLive(
      fakeController({
        running: true,
        connection: "open",
        activeCue: { id: "c1", title: "Sun", body: "About 150 million km away." },
      }),
    );
    expect(screen.getByText("Sun")).toBeInTheDocument();
    expect(screen.getByText(/150 million km/)).toBeInTheDocument();
  });

  it("shows a '+N more' note when cues are queued behind the active one", () => {
    renderLive(
      fakeController({
        running: true,
        connection: "open",
        activeCue: { id: "c1", title: "Sun", body: "About 150 million km away." },
        queuedCues: [
          { id: "c2", title: "Moon", body: "384,400 km away." },
          { id: "c3", title: "Mars", body: "225 million km away." },
        ],
      }),
    );
    // Only the active cue's body renders; the queued ones stay hidden behind it.
    expect(screen.getByText(/150 million km/)).toBeInTheDocument();
    expect(screen.queryByText(/384,400 km/)).not.toBeInTheDocument();
    expect(screen.getByText("+2 more")).toBeInTheDocument();
  });

  it("reflects the active cue level and reports changes", () => {
    const { onCueLevelChange } = renderLive(fakeController());
    const aggressive = screen.getByRole("button", { name: "Aggressive" });
    expect(screen.getByRole("button", { name: "Balanced" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(aggressive);
    expect(onCueLevelChange).toHaveBeenCalledWith("aggressive");
  });
});

describe("LivePanel consent gate", () => {
  beforeEach(() => window.localStorage.clear());

  it("blocks capture behind the recording notice until accepted", () => {
    render(<LivePanel />);
    // Notice is shown; the Record control is not yet present.
    expect(screen.getByText(/Recording notice/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /I understand/i }));

    // Consent accepted → the capture surface (Record button) appears.
    expect(screen.getByRole("button", { name: "Record" })).toBeInTheDocument();
  });
});
