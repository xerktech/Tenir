import { beforeEach, describe, expect, it } from "vitest";

import { clearSessionId, loadSessionId, saveSessionId } from "../src/lib/captureStore";

describe("captureStore", () => {
  beforeEach(() => window.localStorage.clear());

  it("returns null when nothing is stored", () => {
    expect(loadSessionId()).toBeNull();
  });

  it("round-trips a session id and clears it", () => {
    saveSessionId("sess-42");
    expect(loadSessionId()).toBe("sess-42");
    clearSessionId();
    expect(loadSessionId()).toBeNull();
  });
});
