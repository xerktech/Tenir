import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { CUE_EXIT_MS } from "@tenir/client-core";
import { act, fireEvent, render, screen } from "@testing-library/react";
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
  pastCues: [] as { id: string; title: string; body: string; afterSegmentId?: string | null }[],
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

  it("renders the active cue card over the transcript", () => {
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

  it("embeds a released cue inline in the transcript as a collapsed dropdown (XERK-108)", () => {
    renderLive(
      fakeController({
        running: true,
        connection: "open",
        segments: [{ id: "a", text: "hello world" }],
        // A cue that already had its turn in the band, anchored after turn "a".
        pastCues: [{ id: "c1", title: "Sun", body: "About 150 million km away.", afterSegmentId: "a" }],
      }),
    );
    // The past cue shows inline as a collapsed dropdown; its body stays hidden
    // until expanded, so it doesn't interfere with cues still coming in.
    const toggle = screen.getByRole("button", { name: /Sun/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/150 million km/)).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/150 million km/)).toBeInTheDocument();
    // It sits inside the transcript list, after the turn it was anchored to —
    // not up in the live cue band.
    expect(screen.getByText("hello world")).toBeInTheDocument();
  });

  it("wraps the transcript in its own scroll box, but not the empty state (XERK-103)", () => {
    const { container, rerender } = renderLive(fakeController());
    // Idle: the empty state renders directly, with no scroll box to bound.
    expect(container.querySelector(".transcript-scroll")).toBeNull();

    rerender(
      <LiveView
        controller={fakeController({
          running: true,
          connection: "open",
          segments: [{ id: "a", text: "hello world" }],
        })}
        cueLevel="balanced"
        onCueLevelChange={vi.fn()}
      />,
    );
    const box = container.querySelector(".transcript-scroll");
    expect(box).not.toBeNull();
    // The transcript lives inside the scroll box, which sits in the cue stage so
    // the cue floats over it rather than scrolling away with the captions.
    expect(box!.querySelector("ul.transcript")).not.toBeNull();
    expect(box!.closest(".cue-stage")).not.toBeNull();
  });

  it("follows the newest caption while pinned, and releases when scrolled up (XERK-103)", () => {
    const c = fakeController({
      running: true,
      connection: "open",
      segments: [{ id: "a", text: "one" }],
    });
    const { container, rerender } = renderLive(c);
    const box = container.querySelector<HTMLElement>(".transcript-scroll")!;
    Object.defineProperty(box, "scrollHeight", { get: () => 500, configurable: true });
    Object.defineProperty(box, "clientHeight", { get: () => 200, configurable: true });

    const withSegments = (segments: { id: string; text: string }[]) =>
      rerender(
        <LiveView
          controller={fakeController({ running: true, connection: "open", segments })}
          cueLevel="balanced"
          onCueLevelChange={vi.fn()}
        />,
      );

    // A new caption while pinned to the bottom follows to the newest text.
    withSegments([
      { id: "a", text: "one" },
      { id: "b", text: "two" },
    ]);
    expect(box.scrollTop).toBe(500);

    // The viewer scrolls up to re-read earlier text (now far from the bottom).
    box.scrollTop = 0;
    fireEvent.scroll(box);
    // Fresh captions must not yank them back down.
    withSegments([
      { id: "a", text: "one" },
      { id: "b", text: "two" },
      { id: "c", text: "three" },
    ]);
    expect(box.scrollTop).toBe(0);
  });

  it("floats the cue over the transcript without displacing it (XERK-107)", () => {
    const running = { running: true, connection: "open" as const, segments: [{ id: "a", text: "one" }] };
    const { container, rerender } = renderLive(fakeController(running));

    const stage = container.querySelector(".cue-stage")!;
    const box = container.querySelector(".transcript-scroll")!;
    // No cue: the stage holds the transcript box alone.
    expect(stage.children).toHaveLength(1);
    expect(stage.firstElementChild).toBe(box);

    rerender(
      <LiveView
        controller={fakeController({
          ...running,
          activeCue: { id: "c1", title: "Sun", body: "About 150 million km away." },
        })}
        cueLevel="balanced"
        onCueLevelChange={vi.fn()}
      />,
    );

    // The cue joins the same stage *after* the transcript box, so it overlays
    // rather than pushing the captions down: the box is the same element and is
    // still the stage's first child, exactly where it was.
    const band = container.querySelector(".cue-band")!;
    expect(band).not.toBeNull();
    expect(container.querySelector(".transcript-scroll")).toBe(box);
    expect(stage.firstElementChild).toBe(box);
    expect(band.previousElementSibling).toBe(box);
    // The cue never sits in the page flow above the card.
    expect(band.closest(".cue-stage")).toBe(stage);
  });

  it("fades a released cue out before unmounting it (XERK-107)", () => {
    vi.useFakeTimers();
    try {
      const running = {
        running: true,
        connection: "open" as const,
        segments: [{ id: "a", text: "one" }],
      };
      const withCue = (activeCue: { id: string; title: string; body: string } | null) => (
        <LiveView
          controller={fakeController({ ...running, activeCue })}
          cueLevel="balanced"
          onCueLevelChange={vi.fn()}
        />
      );
      const cue = { id: "c1", title: "Sun", body: "About 150 million km away." };
      const { container, rerender } = render(withCue(cue));
      expect(container.querySelector(".cue-band")).not.toBeNull();
      expect(container.querySelector(".cue-band.exiting")).toBeNull();

      // Released (its TTL expired with nothing queued): still painted, now on
      // its way out and hidden from assistive tech.
      rerender(withCue(null));
      const exiting = container.querySelector(".cue-band.exiting");
      expect(exiting).not.toBeNull();
      expect(exiting).toHaveAttribute("aria-hidden", "true");
      expect(screen.getByText("Sun")).toBeInTheDocument();

      // Once the fade has run, it is gone for good.
      act(() => void vi.advanceTimersByTime(CUE_EXIT_MS));
      expect(container.querySelector(".cue-band")).toBeNull();
      expect(screen.queryByText("Sun")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels the fade when a queued cue takes over mid-exit (XERK-107)", () => {
    vi.useFakeTimers();
    try {
      const running = {
        running: true,
        connection: "open" as const,
        segments: [{ id: "a", text: "one" }],
      };
      const withCue = (activeCue: { id: string; title: string; body: string } | null) => (
        <LiveView
          controller={fakeController({ ...running, activeCue })}
          cueLevel="balanced"
          onCueLevelChange={vi.fn()}
        />
      );
      const { container, rerender } = render(
        withCue({ id: "c1", title: "Sun", body: "About 150 million km away." }),
      );
      rerender(withCue(null));
      rerender(withCue({ id: "c2", title: "Moon", body: "384,400 km away." }));

      // The successor shows immediately, not mid-fade, and the pending unmount
      // of its predecessor must not take it down with it.
      expect(container.querySelector(".cue-band.exiting")).toBeNull();
      act(() => void vi.advanceTimersByTime(CUE_EXIT_MS * 4));
      expect(screen.getByText("Moon")).toBeInTheDocument();
      expect(screen.queryByText("Sun")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
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

// jsdom applies no stylesheet, so the overlay itself is asserted at the source
// level: these rules are what actually stop the transcript from being displaced.
describe("cue overlay styling (XERK-107)", () => {
  const css = readFileSync(resolve(process.cwd(), "src/styles.css")).toString("utf8");

  it("takes the cue band out of the flow, over a positioned stage", () => {
    expect(css).toMatch(/\.cue-stage\s*\{[^}]*position:\s*relative/);
    expect(css).toMatch(/\.cue-band\s*\{[^}]*position:\s*absolute/);
    // Click-through band, with the card taking its own touches back so the cue
    // text stays selectable over a scrollable transcript.
    expect(css).toMatch(/\.cue-band\s*\{[^}]*pointer-events:\s*none/);
    expect(css).toMatch(/\.cue-card\s*\{[^}]*pointer-events:\s*auto/);
    // Opaque fill: the captions underneath must not read through the cue.
    expect(css).toMatch(/\.cue-card\s*\{[^}]*background:\s*color-mix\([^;]*--surface-raised/);
    // The band no longer reserves space above the transcript.
    expect(css).not.toMatch(/\.cue-band\s*\{[^}]*margin-bottom/);
  });

  it("fades in and out over --cue-fade, matching the unmount delay", () => {
    expect(css).toMatch(/\.cue-band\s*\{[^}]*transition:\s*opacity var\(--cue-fade\)/);
    expect(css).toMatch(/\.cue-band\.exiting\s*\{[^}]*opacity:\s*0/);
    const token = css.match(/--cue-fade:\s*(\d+)ms/);
    expect(token).not.toBeNull();
    expect(Number(token![1])).toBe(CUE_EXIT_MS);
  });

  it("drops the motion for viewers who ask for reduced motion", () => {
    const reduced = css.match(/@media \(prefers-reduced-motion: reduce\) \{\s*\n\s*\.cue-band[^}]*\}[^}]*\}/);
    expect(reduced).not.toBeNull();
    expect(reduced![0]).toMatch(/animation:\s*none/);
  });
});
