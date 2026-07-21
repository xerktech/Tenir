import { ApiError, type ConversationSummary } from "@tenir/client-core";
import { describe, expect, it } from "vitest";

import {
  conversationLabel,
  errText,
  msToClock,
  overallStatusText,
  statusStateLabel,
} from "../src/lib/format";

describe("errText", () => {
  it("keeps the HTTP status for ApiError and stringifies anything else", () => {
    expect(errText(new ApiError(404, "not found"))).toBe("404: not found");
    expect(errText(new Error("plain"))).toBe("Error: plain");
    expect(errText("raw string")).toBe("raw string");
  });
});

describe("msToClock", () => {
  it("renders a millisecond offset as m:ss", () => {
    expect(msToClock(0)).toBe("0:00");
    expect(msToClock(5_000)).toBe("0:05");
    expect(msToClock(83_000)).toBe("1:23");
    expect(msToClock(3_600_000)).toBe("60:00");
  });

  it("clamps negative values to zero", () => {
    expect(msToClock(-100)).toBe("0:00");
  });
});

describe("conversationLabel", () => {
  const base: ConversationSummary = {
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

  it("summarises timing, turns and status", () => {
    const label = conversationLabel(base);
    expect(label).toContain("5:00");
    expect(label).toContain("12 turns");
    expect(label).toContain("complete");
  });

  it("omits the duration while a session has none yet", () => {
    const label = conversationLabel({ ...base, durationMs: 0 });
    expect(label.split(" · ")).not.toContain("0:00");
    expect(label).toContain("12 turns");
  });
});

describe("status labels", () => {
  it("labels each component state", () => {
    expect(statusStateLabel("ready")).toBe("Ready");
    expect(statusStateLabel("connecting")).toBe("Connecting…");
    expect(statusStateLabel("down")).toBe("Down");
  });

  it("headlines the rolled-up system status", () => {
    expect(overallStatusText("ready")).toBe("All systems ready");
    expect(overallStatusText("degraded")).toBe("Some components degraded");
    expect(overallStatusText("down")).toBe("System down");
  });
});
