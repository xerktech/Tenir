/**
 * Pure helpers for the Tenir phone page — ported from the upstream
 * `even/src/phone/{session,history}.ts` (the logic their jsdom tests covered),
 * kept DOM-free so they unit-test under `bun test` without a renderer.
 */

import type { Conversation, CueView, SegmentView } from "../core/api";
import type { TenirCue, TenirSegment } from "../shared/types";

/** One row of the rendered live transcript: a finalized turn or a reviewed cue. */
export type TranscriptRow =
  | { kind: "segment"; segment: TenirSegment }
  | { kind: "cue"; cue: TenirCue };

/**
 * Interleave finalized turns with reviewed cues (XERK-108): each cue lands
 * right after the turn it was anchored to, and a cue whose anchor is out of
 * range — before any speech, or a turn since scrolled off — leads the
 * transcript.
 */
export function liveTranscriptRows(segments: TenirSegment[], cues: TenirCue[]): TranscriptRow[] {
  const byIndex = new Map<number, TenirCue[]>();
  const leading: TenirCue[] = [];
  for (const cue of cues) {
    if (cue.afterIndex >= 0 && cue.afterIndex < segments.length) {
      const list = byIndex.get(cue.afterIndex);
      if (list) list.push(cue);
      else byIndex.set(cue.afterIndex, [cue]);
    } else {
      leading.push(cue);
    }
  }
  const rows: TranscriptRow[] = leading.map((cue) => ({ kind: "cue" as const, cue }));
  segments.forEach((segment, i) => {
    rows.push({ kind: "segment", segment });
    for (const cue of byIndex.get(i) ?? []) rows.push({ kind: "cue", cue });
  });
  return rows;
}

/** The in-session one-word state, honest about connectivity like the lens. */
export function sessionStatus(connection: "connecting" | "open" | "closed"): string {
  if (connection === "connecting") return "connecting…";
  if (connection === "closed") return "reconnecting…";
  return "listening";
}

/** Render a millisecond span as m:ss (hours folded into minutes). */
export function formatDuration(ms: number): string {
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/** Segment timing rendered as "m:ss–m:ss" offsets from the session start. */
export function segmentTiming(s: SegmentView): string {
  return `${formatDuration(s.startMs)}–${formatDuration(s.endMs)}`;
}

/** A history transcript row: a spoken segment or a private cue on one timeline. */
export type TimelineItem =
  | { kind: "segment"; at: number; seg: SegmentView }
  | { kind: "cue"; at: number; cue: CueView };

/**
 * Merge segments and cues into one timeline, ordered by position (cues sit
 * after the segment they share a timestamp with). An older/partial payload
 * without cues is tolerated rather than crashing.
 */
export function timeline(conv: Conversation): TimelineItem[] {
  const items: TimelineItem[] = [
    ...conv.segments.map((seg) => ({ kind: "segment" as const, at: seg.startMs, seg })),
    ...(conv.cues ?? []).map((cue) => ({ kind: "cue" as const, at: cue.atMs, cue })),
  ];
  return items.sort(
    (a, b) => a.at - b.at || (a.kind === "cue" ? 1 : 0) - (b.kind === "cue" ? 1 : 0),
  );
}
