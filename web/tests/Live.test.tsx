import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { CUE_EXIT_MS, CUE_TTL_MS } from "@tenir/client-core";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CaptureProvider } from "../src/lib/capture";
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
  activeCueEndsAt: null as number | null,
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

function renderLive(controller: CaptureController) {
  return render(<LiveView controller={controller} />);
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

  it("renders a turn's English translation under the original (XERK-160)", () => {
    renderLive(
      fakeController({
        running: true,
        connection: "open",
        segments: [
          { id: "a", text: "hola, ¿qué tal?", lang: "es", translation: "hello, how are you?" },
          { id: "b", text: "sigo aquí", lang: "es" },
        ],
      }),
    );
    expect(screen.getByText("hello, how are you?")).toBeInTheDocument();
    expect(screen.getByTitle("Translated from Spanish")).toHaveTextContent("EN");
    // The translated turn's ORIGINAL text is led by its source-language chip —
    // the counterpart of the "EN" tag.
    expect(screen.getByTitle("Spoken in Spanish")).toHaveTextContent("ES");
    // A turn whose translation hasn't landed yet renders no translation line
    // and no source chip on its original.
    const rows = document.querySelectorAll(".translation");
    expect(rows).toHaveLength(1);
    expect(document.querySelectorAll(".translation-lang")).toHaveLength(2);
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

  it("shows a '+N more' note when cues are queued behind the active one (XERK-102)", () => {
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

  it("shows a grounded cue's source under the body, live and inline (XERK-120)", () => {
    renderLive(
      fakeController({
        running: true,
        connection: "open",
        activeCue: { id: "c1", title: "PM", body: "Andy Burnham took office.", source: "BBC News" },
        segments: [{ id: "a", text: "hello world" }],
        pastCues: [
          { id: "c2", title: "Sun", body: "150M km away.", source: "Wikipedia", afterSegmentId: "a" },
        ],
      }),
    );
    // The live band carries the attribution line.
    expect(screen.getByText("BBC News")).toBeInTheDocument();
    // The inline disclosure reveals its own source with the body.
    fireEvent.click(screen.getByRole("button", { name: /Sun/ }));
    expect(screen.getByText("Wikipedia")).toBeInTheDocument();
  });

  it("renders no source line for an ungrounded cue (XERK-120)", () => {
    const { container } = renderLive(
      fakeController({
        running: true,
        connection: "open",
        activeCue: { id: "c1", title: "Sun", body: "About 150 million km away." },
      }),
    );
    expect(container.querySelector(".cue-card-source")).toBeNull();
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

  it("cancels the fade when a fresher cue takes over mid-exit (XERK-107)", () => {
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

  describe("live cue countdown (XERK-110)", () => {
    const running = { running: true, connection: "open" as const };
    // The countdown reads the cue's wall-clock end time (XERK-159); a cue taking
    // the band opens a fresh window one TTL from now (the boundary-promotion case).
    const withCue = (activeCue: { id: string; title: string; body: string } | null) => (
      <LiveView
        controller={fakeController({
          ...running,
          activeCue,
          activeCueEndsAt: activeCue ? Date.now() + CUE_TTL_MS : null,
        })}
      />
    );
    const cue = { id: "c1", title: "Sun", body: "About 150 million km away." };
    const countdown = (container: HTMLElement) =>
      container.querySelector(".cue-card-countdown")?.textContent;

    it("counts the seconds down to dismissal across from the title", () => {
      vi.useFakeTimers();
      try {
        const { container } = render(withCue(cue));
        // Full TTL the moment the cue lands, in the card's head next to the title.
        expect(countdown(container)).toBe("10s");
        const head = container.querySelector(".cue-card-head")!;
        expect(head.querySelector(".cue-card-title")!.textContent).toBe("Sun");
        expect(head.querySelector(".cue-card-countdown")).not.toBeNull();

        act(() => void vi.advanceTimersByTime(1000));
        expect(countdown(container)).toBe("9s");
        act(() => void vi.advanceTimersByTime(4000));
        expect(countdown(container)).toBe("5s");
        // The last second still reads 1s; it only hits 0 once the TTL is up.
        act(() => void vi.advanceTimersByTime(CUE_TTL_MS - 5000 - 1));
        expect(countdown(container)).toBe("1s");
        act(() => void vi.advanceTimersByTime(1));
        expect(countdown(container)).toBe("0s");
      } finally {
        vi.useRealTimers();
      }
    });

    it("restarts the count for a fresher cue that supersedes the current one", () => {
      vi.useFakeTimers();
      try {
        const { container, rerender } = render(withCue(cue));
        act(() => void vi.advanceTimersByTime(6000));
        expect(countdown(container)).toBe("4s");

        // The next cue takes the band: it gets its own full countdown, not the
        // remainder of its predecessor's.
        rerender(withCue({ id: "c2", title: "Moon", body: "384,400 km away." }));
        act(() => void vi.advanceTimersByTime(0));
        expect(countdown(container)).toBe("10s");
      } finally {
        vi.useRealTimers();
      }
    });

    it("shows a mid-window cue's remaining time, not a fresh ten (XERK-159)", () => {
      vi.useFakeTimers();
      try {
        // A cue whose turn opened while the app was backgrounded is reconciled to
        // the band already partway through its window (endsAt only 3s out). The
        // count must read the truth — 3s — not restart at ten.
        const { container } = render(
          <LiveView
            controller={fakeController({
              ...running,
              activeCue: cue,
              activeCueEndsAt: Date.now() + 3000,
            })}
          />,
        );
        expect(countdown(container)).toBe("3s");
      } finally {
        vi.useRealTimers();
      }
    });

    it("keeps the ticking number out of the card's announcement", () => {
      // The band is an aria-live region, so a number changing every second
      // inside it would re-announce the whole cue once a second.
      const { container } = render(withCue(cue));
      expect(container.querySelector(".cue-band")).toHaveAttribute("aria-live", "polite");
      expect(container.querySelector(".cue-card-countdown")).toHaveAttribute("aria-hidden", "true");
    });
  });

  it("no longer renders a cue aggressiveness toggle (XERK-114)", () => {
    renderLive(fakeController());
    expect(screen.queryByRole("group", { name: /cue detail level/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Aggressive" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Balanced" })).toBeNull();
  });
});

describe("LivePanel consent gate", () => {
  beforeEach(() => window.localStorage.clear());

  it("blocks capture behind the recording notice until accepted", () => {
    // LivePanel reads the shared session from the capture context (XERK-111).
    render(
      <CaptureProvider>
        <LivePanel />
      </CaptureProvider>,
    );
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

  it("puts the countdown across from the title (XERK-110)", () => {
    // One row, title left and countdown right, sharing a baseline.
    expect(css).toMatch(/\.cue-card-head\s*\{[^}]*display:\s*flex/);
    expect(css).toMatch(/\.cue-card-head\s*\{[^}]*justify-content:\s*space-between/);
    // Tabular digits so the count doesn't jitter as it ticks down.
    expect(css).toMatch(/\.cue-card-countdown\s*\{[^}]*font-variant-numeric:\s*tabular-nums/);
  });

  it("drops the motion for viewers who ask for reduced motion", () => {
    const reduced = css.match(/@media \(prefers-reduced-motion: reduce\) \{\s*\n\s*\.cue-band[^}]*\}[^}]*\}/);
    expect(reduced).not.toBeNull();
    expect(reduced![0]).toMatch(/animation:\s*none/);
  });
});
