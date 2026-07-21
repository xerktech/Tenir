/**
 * Small presentation helpers shared by the mobile screens — kept React-Native-free
 * so they're unit-tested directly.
 */

import { ApiError } from "@tenir/client-core";
import type { ComponentState, ConversationSummary, SystemStatus } from "@tenir/client-core";

/** Format any thrown value into a user-facing message (ApiError keeps its status). */
export function errText(err: unknown): string {
  return err instanceof ApiError ? `${err.status}: ${err.message}` : String(err);
}

/** Render a millisecond offset/duration as m:ss (e.g. 83_000 → "1:23"). */
export function msToClock(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/** One-line label for a conversation row in the history list. */
export function conversationLabel(c: ConversationSummary): string {
  const when = new Date(c.startedAt).toLocaleString();
  const parts = [when, `${c.segmentCount} turns`, c.status];
  if (c.durationMs > 0) parts.splice(1, 0, msToClock(c.durationMs));
  return parts.join(" · ");
}

/** Human label for a component's status light. */
export function statusStateLabel(state: ComponentState): string {
  return { ready: "Ready", connecting: "Connecting…", down: "Down" }[state];
}

/** Headline for the whole system's rolled-up status. */
export function overallStatusText(overall: SystemStatus["overall"]): string {
  return {
    ready: "All systems ready",
    degraded: "Some components degraded",
    down: "System down",
  }[overall];
}
