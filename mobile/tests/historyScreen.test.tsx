/**
 * History screen, screen-captured meetings (XERK-2507): the meeting line on a row, the org
 * filter, each segment's speaker and speaker renaming — the parity of web/tests/History.test.tsx.
 * React Native and the UI kit are swapped for plain DOM stand-ins so the screen renders under
 * jsdom; the controller is a stub, and client-core's pure meeting helpers are real.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation, ConversationSummary, OrgView } from "@tenir/client-core";

const { box } = vi.hoisted(() => ({
  box: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
}));

vi.mock("react-native", () => ({
  Text: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
  View: box,
  Switch: () => null,
  Linking: { openURL: vi.fn() },
}));
vi.mock("../src/ui/components", () => ({
  Screen: box,
  Row: ({ children }: { children?: ReactNode }) => <div role="group">{children}</div>,
  ListItem: ({ children }: { children?: ReactNode }) => <div role="listitem">{children}</div>,
  Heading: ({ children }: { children?: ReactNode }) => <h1>{children}</h1>,
  Muted: ({ children }: { children?: ReactNode }) => <p>{children}</p>,
  Badge: ({ children }: { children?: ReactNode }) => <mark>{children}</mark>,
  Spinner: () => null,
  EmptyState: ({ title }: { title: string }) => <p>{title}</p>,
  LangTagText: () => null,
  TranslationText: () => null,
  Button: ({ title, onPress, kind }: { title: string; onPress: () => void; kind?: string }) => (
    <button aria-pressed={kind === "primary"} onClick={onPress}>
      {title}
    </button>
  ),
  ConfirmButton: ({ title }: { title: string }) => <button>{title}</button>,
  Field: (props: { placeholder: string; value: string; onChangeText: (t: string) => void }) => (
    <input
      aria-label={props.placeholder}
      value={props.value}
      onChange={(e) => props.onChangeText(e.target.value)}
    />
  ),
}));
vi.mock("../src/ui/ThemeContext", () => ({ useTheme: () => ({ colors: {} }) }));
vi.mock("../src/ui/cue", () => ({ CueDisclosure: () => null }));
vi.mock("../src/ui/AudioPlayer", () => ({ AudioPlayer: () => null }));
vi.mock("../src/native/audioPlayer", () => ({ audioPlayerAvailable: false }));
const notify = vi.hoisted(() => vi.fn());
vi.mock("../src/lib/notify", () => ({ useNotify: () => notify }));

const ctrl = vi.hoisted(() => ({
  state: {} as Record<string, unknown>,
}));
vi.mock("../src/lib/controllers", () => ({ useHistory: () => ctrl.state }));

import { HistoryScreen } from "../src/screens/History";

const summary = (over: Partial<ConversationSummary> = {}): ConversationSummary => ({
  id: "c1",
  status: "ready",
  micSource: "phone-microphone",
  sourceLang: "en",
  startedAt: "2026-06-16T18:00:00Z",
  endedAt: "2026-06-16T18:02:05Z",
  durationMs: 125_000,
  segmentCount: 2,
  hasAudio: false,
  ...over,
});
const meeting = (over: Partial<ConversationSummary> = {}) =>
  summary({
    id: "m1",
    micSource: null,
    source: "screen",
    orgSiteKey: "northwind",
    orgName: "Northwind",
    platform: "zoom",
    meetingTitle: "Q3 Release Sync",
    ...over,
  });
const detail = (speaker = "sam@example.com"): Conversation => ({
  ...meeting(),
  segments: [
    { segmentId: "s1", text: "ship it friday", startMs: 0, endMs: 900, lang: "en", speaker },
    { segmentId: "s2", text: "agreed", startMs: 1000, endMs: 1900, lang: "en", speaker: "Priya Raman" },
  ],
  cues: [],
  participants: [speaker, "Priya Raman"],
});

function setCtrl(data: ConversationSummary[], over: Record<string, unknown> = {}) {
  ctrl.state = {
    data,
    error: null,
    loading: false,
    reload: vi.fn(),
    search: "",
    setSearch: vi.fn(),
    org: "",
    setOrg: vi.fn(),
    orgs: [] as OrgView[],
    open: vi.fn(),
    remove: vi.fn(),
    renameSpeaker: vi.fn(),
    ...over,
  };
  return ctrl.state as Record<string, ReturnType<typeof vi.fn>>;
}

beforeEach(() => notify.mockReset());

describe("HistoryScreen meetings (XERK-2507)", () => {
  it("shows the meeting title, org badge and platform on a meeting row only", () => {
    setCtrl([meeting(), summary()]);
    render(<HistoryScreen />);
    const [meetingRow, micRow] = screen.getAllByRole("listitem");
    expect(within(meetingRow).getByText("Q3 Release Sync")).toBeTruthy();
    expect(within(meetingRow).getByText("Northwind").tagName).toBe("MARK");
    expect(within(meetingRow).getByText("Zoom")).toBeTruthy();
    expect(within(micRow).queryByText("Zoom")).toBeNull();
  });

  it("offers the org filter only when there are orgs, and sets the chosen one", () => {
    setCtrl([summary()]);
    const { unmount } = render(<HistoryScreen />);
    expect(screen.queryByRole("button", { name: "All orgs" })).toBeNull();
    unmount();

    const c = setCtrl([meeting()], {
      org: "northwind",
      orgs: [
        { siteKey: "acme", name: "Acme" },
        { siteKey: "northwind", name: "Northwind" },
      ],
    });
    render(<HistoryScreen />);
    expect(screen.getByRole("button", { name: "Northwind" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "All orgs" }).getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "Acme" }));
    expect(c.setOrg).toHaveBeenCalledWith("acme");
    fireEvent.click(screen.getByRole("button", { name: "All orgs" }));
    expect(c.setOrg).toHaveBeenLastCalledWith("");
  });

  it("shows each segment's speaker and renames a speaker", async () => {
    const c = setCtrl([meeting()]);
    c.open.mockResolvedValue(detail());
    c.renameSpeaker.mockResolvedValue(detail("Sam"));
    render(<HistoryScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await screen.findByRole("heading", { name: "Q3 Release Sync" });
    expect(screen.getByText(/ship it friday/).textContent).toContain("sam@example.com: ship it friday");

    const samRow = screen.getAllByRole("group").find((g) => g.textContent?.startsWith("sam@"))!;
    fireEvent.click(within(samRow).getByRole("button", { name: "Rename" }));
    fireEvent.change(screen.getByLabelText("New name for sam@example.com"), {
      target: { value: " Sam " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(c.renameSpeaker).toHaveBeenCalledWith("m1", "sam@example.com", "Sam"),
    );
    await waitFor(() =>
      expect(screen.getByText(/ship it friday/).textContent).toContain("Sam: ship it friday"),
    );
    expect(notify).toHaveBeenCalledWith("Renamed to Sam");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("keeps the editor open and reports a failed rename", async () => {
    const c = setCtrl([meeting()]);
    c.open.mockResolvedValue(detail());
    c.renameSpeaker.mockRejectedValue(new Error("no such speaker in this meeting"));
    render(<HistoryScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await screen.findByRole("heading", { name: "Q3 Release Sync" });
    const priyaRow = screen.getAllByRole("group").find((g) => g.textContent?.startsWith("Priya"))!;
    fireEvent.click(within(priyaRow).getByRole("button", { name: "Rename" }));
    fireEvent.change(screen.getByLabelText("New name for Priya Raman"), { target: { value: "Priya" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith(expect.stringMatching(/no such speaker/), "err"));
    expect(screen.getByLabelText("New name for Priya Raman")).toBeTruthy();
  });

  it("does not offer speakers on a mic session", async () => {
    const c = setCtrl([summary()]);
    c.open.mockResolvedValue({ ...summary(), segments: [], cues: [] });
    render(<HistoryScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await screen.findByRole("heading", { name: "Session detail" });
    expect(screen.queryByText("Speakers")).toBeNull();
  });
});
