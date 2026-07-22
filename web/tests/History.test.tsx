import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { formatDuration, HistoryPanel } from "../src/panels/History";
import { ToastProvider } from "../src/lib/toast";

const list = vi.fn();
const get = vi.fn();
const remove = vi.fn();

vi.mock("@tenir/client-core", () => ({
  ApiError: class ApiError extends Error {},
  history: {
    list: (q?: string) => list(q),
    get: (id: string) => get(id),
    remove: (id: string) => remove(id),
    audioUrl: (id: string) => `/conversations/${id}/audio`,
  },
}));

const summary = (over: Record<string, unknown> = {}) => ({
  id: "c1",
  status: "stored",
  micSource: "phone-microphone",
  sourceLang: "en",
  startedAt: "2026-06-16T18:00:00Z",
  endedAt: "2026-06-16T18:02:05Z",
  durationMs: 125_000,
  segmentCount: 12,
  hasAudio: false,
  ...over,
});

beforeEach(() => {
  list.mockReset();
  get.mockReset();
  remove.mockReset();
});

function renderPanel() {
  return render(
    <ToastProvider>
      <HistoryPanel />
    </ToastProvider>,
  );
}

describe("formatDuration", () => {
  it("renders millisecond spans as m:ss", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(65_000)).toBe("1:05");
    expect(formatDuration(125_000)).toBe("2:05");
  });
});

describe("HistoryPanel", () => {
  it("shows the empty state when there are no conversations", async () => {
    list.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => expect(screen.getByText("No conversations yet")).toBeInTheDocument());
  });

  it("surfaces a failed listing instead of rendering an empty page", async () => {
    // A 500 from the api used to be swallowed, leaving a blank section that read as
    // "you have no conversations" — the reported symptom in XERK-58.
    list.mockRejectedValueOnce(new Error("Internal Server Error"));
    renderPanel();
    await waitFor(() => expect(screen.getByText("Could not load history")).toBeInTheDocument());
    expect(screen.getByText(/Internal Server Error/)).toBeInTheDocument();
    expect(screen.queryByText("No conversations yet")).not.toBeInTheDocument();

    // Retry re-runs the listing and clears the error once it succeeds.
    list.mockResolvedValue([summary()]);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("row", { name: /stored/ });
    expect(screen.queryByText("Could not load history")).not.toBeInTheDocument();
  });

  it("lists conversations with date, duration, turns and status columns", async () => {
    list.mockResolvedValue([summary()]);
    renderPanel();
    const row = await screen.findByRole("row", { name: /stored/ });
    expect(within(row).getByText("2:05")).toBeInTheDocument();
    expect(within(row).getByText("12")).toBeInTheDocument();
    expect(within(row).getByText("stored")).toBeInTheDocument();
    // The date leads the row as the opener link.
    expect(
      within(row).getByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }),
    ).toBeInTheDocument();
  });

  it("opens a detail with segments, timing and a native audio player", async () => {
    list.mockResolvedValue([summary({ hasAudio: true })]);
    get.mockResolvedValue({
      ...summary({ hasAudio: true }),
      segments: [
        { segmentId: "s1", text: "hello there", startMs: 0, endMs: 1500, lang: "en" },
        { segmentId: "s2", text: "how are you", startMs: 61_000, endMs: 65_000, lang: "en" },
      ],
    });
    const { container } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));

    await screen.findByText(/hello there/);
    expect(get).toHaveBeenCalledWith("c1");
    // Segment timing offsets render alongside the text.
    expect(screen.getByText("0:00–0:02")).toBeInTheDocument();
    expect(screen.getByText("1:01–1:05")).toBeInTheDocument();
    expect(screen.getByText(/how are you/)).toBeInTheDocument();
    // Retained audio plays inline via a native <audio controls> element (with its
    // seek bar), pointed at the audio endpoint (XERK-67).
    const player = container.querySelector("audio");
    expect(player).not.toBeNull();
    expect(player).toHaveAttribute("controls");
    expect(player).toHaveAttribute("src", "/conversations/c1/audio");
    // …and remains downloadable.
    expect(screen.getByRole("link", { name: "Download audio.wav" })).toHaveAttribute(
      "href",
      "/conversations/c1/audio",
    );
  });

  it("opens the transcript as its own page, replacing the list, with a back button", async () => {
    // The detail used to render inline at the bottom of the list; it now takes over
    // the panel as its own page, so the transcript isn't lost below the fold (XERK-65).
    list.mockResolvedValue([summary()]);
    get.mockResolvedValue({ ...summary(), segments: [] });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));

    await screen.findByText("Conversation detail");
    // The list is gone — the detail is the whole view now.
    expect(screen.queryByText("History & search")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    // Back returns to the list without re-fetching the detail.
    fireEvent.click(screen.getByRole("button", { name: "← History" }));
    await screen.findByText("History & search");
    expect(screen.queryByText("Conversation detail")).not.toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("says so when the session has no transcript", async () => {
    // A session that stored no turns used to open a detail with an empty body,
    // indistinguishable from the link doing nothing (XERK-58).
    list.mockResolvedValue([summary({ segmentCount: 0 })]);
    get.mockResolvedValue({ ...summary({ segmentCount: 0 }), segments: [] });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));
    await screen.findByText("Conversation detail");
    expect(screen.getByText("No transcript was recorded for this session.")).toBeInTheDocument();
  });

  it("omits the audio player when no audio was retained", async () => {
    list.mockResolvedValue([summary()]);
    get.mockResolvedValue({ ...summary(), segments: [] });
    const { container } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));
    await screen.findByText("Conversation detail");
    expect(container.querySelector("audio")).toBeNull();
    expect(screen.queryByRole("link", { name: "Download audio.wav" })).not.toBeInTheDocument();
  });

  it("renders inline cue boxes and opens a popup with the detail", async () => {
    list.mockResolvedValue([summary()]);
    get.mockResolvedValue({
      ...summary(),
      segments: [{ segmentId: "s1", text: "how far is the sun", startMs: 0, endMs: 1500, lang: "en" }],
      cues: [{ cueId: "cue-1", title: "Sun", body: "About 150 million km away.", atMs: 1500 }],
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));

    // The cue shows inline as a clickable box with its title.
    const cueBox = await screen.findByRole("button", { name: /Sun/ });
    // The body isn't shown until the popup opens.
    expect(screen.queryByText(/150 million km/)).not.toBeInTheDocument();

    fireEvent.click(cueBox);
    const dialog = await screen.findByRole("dialog", { name: "Sun" });
    expect(within(dialog).getByText(/150 million km/)).toBeInTheDocument();

    // Closing the popup returns to the transcript without navigating away.
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText(/how far is the sun/)).toBeInTheDocument();
  });

  it("deletes a conversation from its row", async () => {
    list.mockResolvedValue([summary()]);
    remove.mockResolvedValue(undefined);
    renderPanel();
    const row = await screen.findByRole("row", { name: /stored/ });
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("c1"));
  });

  it("sorts rows when a column header is clicked", async () => {
    list.mockResolvedValue([
      summary({ id: "a", segmentCount: 5 }),
      summary({ id: "b", segmentCount: 20 }),
    ]);
    renderPanel();
    await screen.findAllByText("stored");

    const turnsHeader = screen.getByRole("button", { name: /Turns/ });

    // First click on "Turns" sorts ascending: 5 before 20.
    fireEvent.click(turnsHeader);
    let bodyRows = screen.getAllByRole("row").slice(1);
    expect(within(bodyRows[0]).getByText("5")).toBeInTheDocument();
    expect(within(bodyRows[1]).getByText("20")).toBeInTheDocument();

    // Second click flips to descending: 20 before 5.
    fireEvent.click(turnsHeader);
    bodyRows = screen.getAllByRole("row").slice(1);
    expect(within(bodyRows[0]).getByText("20")).toBeInTheDocument();
    expect(within(bodyRows[1]).getByText("5")).toBeInTheDocument();
  });
});
