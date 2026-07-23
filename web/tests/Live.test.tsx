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
  cues: [] as { id: string; title: string; body: string }[],
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

  it("renders live cue cards above the transcript", () => {
    renderLive(
      fakeController({
        running: true,
        connection: "open",
        cues: [{ id: "c1", title: "Sun", body: "About 150 million km away." }],
      }),
    );
    expect(screen.getByText("Sun")).toBeInTheDocument();
    expect(screen.getByText(/150 million km/)).toBeInTheDocument();
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
