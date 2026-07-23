/**
 * Persisted "cue aggressiveness" preference for web (XERK-81).
 *
 * A single global toggle governs how eagerly the api surfaces private context
 * cues. It's remembered in localStorage and sent to the server on session.start,
 * so the choice sticks across sessions and page reloads.
 */

import type { CueLevel } from "@tenir/client-core";

const KEY = "tenir.cue.level";
const DEFAULT: CueLevel = "balanced";
const LEVELS: CueLevel[] = ["conservative", "balanced", "aggressive"];

function isLevel(v: string | null): v is CueLevel {
  return v != null && (LEVELS as string[]).includes(v);
}

export function loadCueLevel(): CueLevel {
  try {
    const v = window.localStorage.getItem(KEY);
    return isLevel(v) ? v : DEFAULT;
  } catch {
    return DEFAULT;
  }
}

export function saveCueLevel(level: CueLevel): void {
  try {
    window.localStorage.setItem(KEY, level);
  } catch {
    /* localStorage unavailable (private mode) — preference is best-effort. */
  }
}

export const CUE_LEVELS = LEVELS;
