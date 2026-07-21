import { renderHook, waitFor } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAuth, useHistory, useStatus } from "../src/lib/controllers";

const api = vi.hoisted(() => ({
  me: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  getStatus: vi.fn(),
  historyList: vi.fn(),
  historyGet: vi.fn(),
  historyRemove: vi.fn(),
}));

const { NetworkError } = vi.hoisted(() => ({
  NetworkError: class NetworkError extends Error {},
}));

vi.mock("@tenir/client-core", () => ({
  me: api.me,
  login: api.login,
  logout: api.logout,
  getStatus: api.getStatus,
  NetworkError,
  history: {
    list: api.historyList,
    get: api.historyGet,
    remove: api.historyRemove,
    audioUrl: vi.fn(),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useAuth", () => {
  it("exposes the principal once authenticated", async () => {
    api.me.mockResolvedValue({ userId: "u", username: "ada", household: "h", role: "owner" });
    const { result } = renderHook(() => useAuth());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.household).toBe("h");
  });

  it("yields null (login required) when /auth/me rejects", async () => {
    api.me.mockRejectedValue(new Error("401"));
    const { result } = renderHook(() => useAuth());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
  });

  it("logs in then re-checks identity", async () => {
    api.me.mockResolvedValueOnce(null).mockResolvedValue({
      userId: "ada",
      username: "ada",
      household: "lab",
      role: "owner",
    });
    api.login.mockResolvedValue(undefined);
    const { result } = renderHook(() => useAuth());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.signIn("ada", "pw");
    });

    expect(api.login).toHaveBeenCalledWith("ada", "pw");
    await waitFor(() => expect(result.current.data?.userId).toBe("ada"));
  });
});

describe("useHistory", () => {
  const summary = {
    id: "c1",
    status: "complete",
    micSource: "phone-microphone",
    sourceLang: "en",
    startedAt: "2026-06-17T10:00:00Z",
    endedAt: "2026-06-17T10:05:00Z",
    durationMs: 300_000,
    segmentCount: 12,
    hasAudio: true,
  };

  it("lists sessions and re-queries when the search changes", async () => {
    api.historyList.mockResolvedValue([summary]);
    const { result } = renderHook(() => useHistory());
    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(api.historyList).toHaveBeenCalledWith(undefined);

    act(() => result.current.setSearch("budget"));
    await waitFor(() => expect(api.historyList).toHaveBeenCalledWith("budget"));
  });

  it("surfaces a failed listing rather than reporting an empty history", async () => {
    // The screen renders "could not load" off this error; without it a failing api
    // is indistinguishable from having recorded nothing (XERK-58).
    api.historyList.mockRejectedValueOnce(new Error("Internal Server Error"));
    const { result } = renderHook(() => useHistory());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect((result.current.error as Error).message).toBe("Internal Server Error");
    expect(result.current.data).toBeNull();
  });

  it("opens a session by id", async () => {
    api.historyList.mockResolvedValue([summary]);
    api.historyGet.mockResolvedValue({ ...summary, segments: [] });
    const { result } = renderHook(() => useHistory());
    await waitFor(() => expect(result.current.loading).toBe(false));

    const conv = await result.current.open("c1");
    expect(api.historyGet).toHaveBeenCalledWith("c1");
    expect(conv.id).toBe("c1");
  });

  it("deletes a session then reloads the list", async () => {
    api.historyList.mockResolvedValue([summary]);
    api.historyRemove.mockResolvedValue(undefined);
    const { result } = renderHook(() => useHistory());
    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(api.historyList).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.remove("c1");
    });

    expect(api.historyRemove).toHaveBeenCalledWith("c1");
    await waitFor(() => expect(api.historyList).toHaveBeenCalledTimes(2)); // reloaded
  });
});

describe("useStatus", () => {
  it("loads the system status snapshot", async () => {
    api.getStatus.mockResolvedValue({
      overall: "ready",
      generatedAt: "x",
      reasons: [],
      components: [{ id: "stt", label: "STT", category: "model", state: "ready", detail: "", checkedAt: "x" }],
    });
    const { result } = renderHook(() => useStatus());
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.unreachable).toBe(false);
    expect(result.current.status?.components[0].id).toBe("stt");
  });

  it("flags the server unreachable on a NetworkError", async () => {
    api.getStatus.mockRejectedValue(new NetworkError("could not reach the server"));
    const { result } = renderHook(() => useStatus());
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.unreachable).toBe(true);
  });
});
