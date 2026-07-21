import { renderHook, waitFor } from "@testing-library/react";
import { act } from "react";
import { describe, expect, it, vi } from "vitest";

import { useAsync } from "../src/lib/useAsync";

describe("useAsync", () => {
  it("loads data and clears the loading flag", async () => {
    const { result } = renderHook(() => useAsync(() => Promise.resolve(42)));
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBe(42);
    expect(result.current.error).toBeNull();
  });

  it("captures a rejection as error", async () => {
    const { result } = renderHook(() => useAsync(() => Promise.reject(new Error("nope"))));
    await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
    expect(result.current.data).toBeNull();
  });

  it("re-runs the loader on reload()", async () => {
    const loader = vi.fn().mockResolvedValue("v");
    const { result } = renderHook(() => useAsync(loader));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(loader).toHaveBeenCalledTimes(1);
    act(() => result.current.reload());
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(2));
  });
});
