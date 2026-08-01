import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { formatDuration, HistoryPanel } from "../src/panels/History";
import { ToastProvider } from "../src/lib/toast";

const list = vi.fn();
const get = vi.fn();
const remove = vi.fn();

vi.mock("@tenir/client-core", () => ({
  ApiError: class ApiError extends Error {},
  langName: (lang?: string | null) =>
    ({ en: "English", es: "Spanish", fr: "French", de: "German", pt: "Portuguese", it: "Italian" })[
      lang ?? ""
    ],
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

  it("renders a stored turn's English translation under the original (XERK-160)", async () => {
    list.mockResolvedValue([summary()]);
    get.mockResolvedValue({
      ...summary(),
      segments: [
        {
          segmentId: "s1",
          text: "hola, ¿qué tal?",
          startMs: 0,
          endMs: 1500,
          lang: "es",
          translation: "hello, how are you?",
        },
        { segmentId: "s2", text: "untranslated turn", startMs: 2000, endMs: 3000, lang: "en" },
      ],
    });
    const { container } = renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));

    await screen.findByText(/hola/);
    expect(screen.getByText("hello, how are you?")).toBeInTheDocument();
    expect(screen.getByTitle("Translated from Spanish")).toHaveTextContent("EN");
    // The translated turn's ORIGINAL text is led by its source-language chip —
    // the counterpart of the "EN" tag; the untranslated turn carries neither.
    expect(screen.getByTitle("Spoken in Spanish")).toHaveTextContent("ES");
    // Only the translated turn carries a translation line.
    expect(container.querySelectorAll(".translation")).toHaveLength(1);
    expect(container.querySelectorAll(".translation-lang")).toHaveLength(2);
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

  it("renders inline cues as dropdowns that expand in place, collapsed by default (XERK-105)", async () => {
    list.mockResolvedValue([summary()]);
    get.mockResolvedValue({
      ...summary(),
      segments: [{ segmentId: "s1", text: "how far is the sun", startMs: 0, endMs: 1500, lang: "en" }],
      cues: [{ cueId: "cue-1", title: "Sun", body: "About 150 million km away.", atMs: 1500 }],
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));

    // The cue shows inline as a collapsed dropdown with its title.
    const cueToggle = await screen.findByRole("button", { name: /Sun/ });
    expect(cueToggle).toHaveAttribute("aria-expanded", "false");
    // The body is hidden until the dropdown is expanded — and there is no popup.
    expect(screen.queryByText(/150 million km/)).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    // Expanding reveals the body inline, in place — no modal.
    fireEvent.click(cueToggle);
    expect(cueToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/150 million km/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // The transcript stays put behind the expanded cue.
    expect(screen.getByText(/how far is the sun/)).toBeInTheDocument();

    // Collapsing hides the body again.
    fireEvent.click(cueToggle);
    expect(cueToggle).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => expect(screen.queryByText(/150 million km/)).not.toBeInTheDocument());
  });

  it("renders a recognized song inline in the transcript timeline (XERK-184)", async () => {
    list.mockResolvedValue([summary()]);
    get.mockResolvedValue({
      ...summary(),
      segments: [{ segmentId: "s1", text: "nice track", startMs: 0, endMs: 1500, lang: "en" }],
      cues: [],
      songs: [
        { songId: "song-1", title: "Weird Fishes", artist: "Radiohead", atMs: 1600, durationMs: 318000 },
      ],
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));
    // The song sits inline as "ARTIST — TITLE" at the point it played.
    expect(await screen.findByText("Radiohead — Weird Fishes")).toBeInTheDocument();
  });

  it("shows a grounded cue's source in the expanded dropdown (XERK-120)", async () => {
    list.mockResolvedValue([summary()]);
    get.mockResolvedValue({
      ...summary(),
      segments: [{ segmentId: "s1", text: "who is the PM", startMs: 0, endMs: 1500, lang: "en" }],
      cues: [
        {
          cueId: "cue-1",
          title: "Prime Minister",
          body: "Andy Burnham took office.",
          atMs: 1500,
          source: "BBC News",
        },
      ],
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));

    fireEvent.click(await screen.findByRole("button", { name: /Prime Minister/ }));
    expect(screen.getByText("BBC News")).toBeInTheDocument();
  });

  it("hides inline cues via the Show-cues toggle, default on (XERK-104)", async () => {
    list.mockResolvedValue([summary()]);
    get.mockResolvedValue({
      ...summary(),
      segments: [{ segmentId: "s1", text: "how far is the sun", startMs: 0, endMs: 1500, lang: "en" }],
      cues: [{ cueId: "cue-1", title: "Sun", body: "About 150 million km away.", atMs: 1500 }],
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));

    // The toggle is offered (there are cues) and defaults to on, so the cue shows.
    const toggle = await screen.findByRole("checkbox", { name: /Show cues/ });
    expect(toggle).toBeChecked();
    expect(screen.getByRole("button", { name: /Sun/ })).toBeInTheDocument();

    // Turning it off drops the cue, leaving the transcript uninterrupted.
    fireEvent.click(toggle);
    expect(toggle).not.toBeChecked();
    expect(screen.queryByRole("button", { name: /Sun/ })).not.toBeInTheDocument();
    expect(screen.getByText(/how far is the sun/)).toBeInTheDocument();

    // Turning it back on restores the cue.
    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: /Sun/ })).toBeInTheDocument();
  });

  it("omits the Show-cues toggle when a conversation has no cues (XERK-104)", async () => {
    list.mockResolvedValue([summary()]);
    get.mockResolvedValue({
      ...summary(),
      segments: [{ segmentId: "s1", text: "just talking", startMs: 0, endMs: 1500, lang: "en" }],
      cues: [],
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: new Date("2026-06-16T18:00:00Z").toLocaleString() }));

    await screen.findByText(/just talking/);
    expect(screen.queryByRole("checkbox", { name: /Show cues/ })).not.toBeInTheDocument();
  });

  it("deletes a conversation from its row via arm-then-confirm", async () => {
    list.mockResolvedValue([summary()]);
    remove.mockResolvedValue(undefined);
    renderPanel();
    const row = await screen.findByRole("row", { name: /stored/ });
    // First click only arms the destructive control…
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    expect(remove).not.toHaveBeenCalled();
    // …the second click commits the delete.
    fireEvent.click(within(row).getByRole("button", { name: "Confirm delete" }));
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
