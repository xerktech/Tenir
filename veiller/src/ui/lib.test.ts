/**
 * Pure phone-page helpers — ports of the logic upstream covered in
 * `even/tests/session.test.ts` (liveTranscriptRows) and
 * `even/tests/history.test.ts` (formatDuration, timeline).
 */

import { describe, expect, it } from "bun:test";

import type { Conversation } from "../core/api";
import { formatDuration, liveTranscriptRows, segmentTiming, sessionStatus, timeline } from "./lib";

describe("liveTranscriptRows", () => {
  const seg = (id: string, text: string) => ({ id, text });
  const cue = (id: string, afterIndex: number) => ({
    id,
    title: `t-${id}`,
    body: "b",
    afterIndex,
  });

  it("interleaves cues after their anchor turn", () => {
    const rows = liveTranscriptRows([seg("a", "one"), seg("b", "two")], [cue("c1", 0)]);
    expect(rows.map((r) => (r.kind === "segment" ? r.segment.id : r.cue.id))).toEqual([
      "a",
      "c1",
      "b",
    ]);
  });

  it("floats an out-of-range cue to the head of the transcript", () => {
    const rows = liveTranscriptRows([seg("a", "one")], [cue("early", -1), cue("gone", 7)]);
    expect(rows.map((r) => (r.kind === "segment" ? r.segment.id : r.cue.id))).toEqual([
      "early",
      "gone",
      "a",
    ]);
  });

  it("keeps multiple cues on one anchor in arrival order", () => {
    const rows = liveTranscriptRows([seg("a", "one")], [cue("c1", 0), cue("c2", 0)]);
    expect(rows.map((r) => (r.kind === "segment" ? r.segment.id : r.cue.id))).toEqual([
      "a",
      "c1",
      "c2",
    ]);
  });
});

describe("sessionStatus", () => {
  it("mirrors the lens's honesty about connectivity", () => {
    expect(sessionStatus("connecting")).toBe("connecting…");
    expect(sessionStatus("closed")).toBe("reconnecting…");
    expect(sessionStatus("open")).toBe("listening");
  });
});

describe("formatDuration / segmentTiming", () => {
  it("renders m:ss with hours folded into minutes", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(61_000)).toBe("1:01");
    expect(formatDuration(3_723_000)).toBe("62:03");
  });

  it("renders segment timing as a start–end range", () => {
    expect(
      segmentTiming({ segmentId: "s", text: "", startMs: 1000, endMs: 2500, lang: null }),
    ).toBe("0:01–0:03");
  });
});

describe("timeline", () => {
  const conv = (over: Partial<Conversation>): Conversation => ({
    id: "c1",
    status: "stored",
    micSource: null,
    sourceLang: null,
    startedAt: "2026-01-01T00:00:00Z",
    endedAt: null,
    durationMs: 0,
    segmentCount: 0,
    hasAudio: false,
    segments: [],
    cues: [],
    ...over,
  });

  it("orders segments and cues by position, cue after its co-timed segment", () => {
    const items = timeline(
      conv({
        segments: [
          { segmentId: "s1", text: "one", startMs: 0, endMs: 900, lang: null },
          { segmentId: "s2", text: "two", startMs: 2000, endMs: 2900, lang: null },
        ],
        cues: [{ cueId: "q1", title: "T", body: "B", atMs: 2000 }],
      }),
    );
    expect(items.map((i) => (i.kind === "segment" ? i.seg.segmentId : i.cue.cueId))).toEqual([
      "s1",
      "s2",
      "q1",
    ]);
  });

  it("tolerates a payload without cues", () => {
    const items = timeline(
      conv({
        segments: [{ segmentId: "s1", text: "one", startMs: 0, endMs: 900, lang: null }],
        cues: undefined as unknown as Conversation["cues"],
      }),
    );
    expect(items).toHaveLength(1);
  });
});
