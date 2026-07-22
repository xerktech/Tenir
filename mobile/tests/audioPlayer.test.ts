import { describe, expect, it } from "vitest";

import {
  initialPlayerState,
  playerReducer,
  progressFraction,
  seekTargetMs,
  type PlayerState,
} from "../src/lib/audioPlayer";

/** Drive the reducer through a sequence of events from the initial state. */
function run(...events: Parameters<typeof playerReducer>[1][]): PlayerState {
  return events.reduce(playerReducer, initialPlayerState);
}

describe("playerReducer", () => {
  it("loads then reports a known duration as ready", () => {
    const state = run({ type: "load" }, { type: "loaded", durationMs: 5000 });
    expect(state).toMatchObject({ status: "ready", durationMs: 5000, positionMs: 0, error: null });
  });

  it("load resets a stale position/duration/error from a prior clip", () => {
    const dirty: PlayerState = { status: "error", positionMs: 4000, durationMs: 5000, error: "boom" };
    expect(playerReducer(dirty, { type: "load" })).toEqual({
      status: "loading",
      positionMs: 0,
      durationMs: 0,
      error: null,
    });
  });

  it("play/pause are optimistic once loaded", () => {
    const ready = run({ type: "load" }, { type: "loaded", durationMs: 5000 });
    expect(playerReducer(ready, { type: "play" }).status).toBe("playing");
    const playing = playerReducer(ready, { type: "play" });
    expect(playerReducer(playing, { type: "pause" }).status).toBe("paused");
  });

  it("ignores play before anything is loaded", () => {
    expect(playerReducer(initialPlayerState, { type: "play" })).toEqual(initialPlayerState);
    const loading = playerReducer(initialPlayerState, { type: "load" });
    expect(playerReducer(loading, { type: "play" }).status).toBe("loading");
  });

  it("derives playing/paused/ended from the authoritative native tick", () => {
    const ready = run({ type: "load" }, { type: "loaded", durationMs: 8000 });

    const playing = playerReducer(ready, {
      type: "tick",
      positionMs: 2000,
      durationMs: 8000,
      playing: true,
      ended: false,
    });
    expect(playing).toMatchObject({ status: "playing", positionMs: 2000 });

    const paused = playerReducer(playing, {
      type: "tick",
      positionMs: 2000,
      durationMs: 8000,
      playing: false,
      ended: false,
    });
    expect(paused.status).toBe("paused");

    const ended = playerReducer(playing, {
      type: "tick",
      positionMs: 8000,
      durationMs: 8000,
      playing: false,
      ended: true,
    });
    expect(ended).toMatchObject({ status: "ended", positionMs: 8000 });
  });

  it("keeps the last known duration when a tick reports 0, and clamps position", () => {
    const ready = run({ type: "load" }, { type: "loaded", durationMs: 6000 });
    const tick = playerReducer(ready, {
      type: "tick",
      positionMs: 9999, // overruns the clip — clamp to duration
      durationMs: 0, // player briefly reports unknown length
      playing: true,
      ended: false,
    });
    expect(tick).toMatchObject({ durationMs: 6000, positionMs: 6000 });
  });

  it("play from ended replays from the start", () => {
    const ended = run(
      { type: "load" },
      { type: "loaded", durationMs: 5000 },
      { type: "tick", positionMs: 5000, durationMs: 5000, playing: false, ended: true },
    );
    expect(ended.status).toBe("ended");
    const replay = playerReducer(ended, { type: "play" });
    expect(replay).toMatchObject({ status: "playing", positionMs: 0 });
  });

  it("seek clamps to the clip and re-arms a finished clip as paused", () => {
    const ended = run(
      { type: "load" },
      { type: "loaded", durationMs: 5000 },
      { type: "tick", positionMs: 5000, durationMs: 5000, playing: false, ended: true },
    );
    const scrubbed = playerReducer(ended, { type: "seek", positionMs: 20_000 });
    expect(scrubbed).toMatchObject({ status: "paused", positionMs: 5000 });

    const back = playerReducer(scrubbed, { type: "seek", positionMs: -100 });
    expect(back.positionMs).toBe(0);
  });

  it("captures an error message and resets to idle", () => {
    const errored = playerReducer(initialPlayerState, { type: "error", message: "network down" });
    expect(errored).toMatchObject({ status: "error", error: "network down" });
    expect(playerReducer(errored, { type: "reset" })).toEqual(initialPlayerState);
  });
});

describe("progressFraction", () => {
  it("is 0 while the duration is unknown", () => {
    expect(progressFraction(initialPlayerState)).toBe(0);
  });

  it("is the clamped position/duration ratio", () => {
    expect(progressFraction({ status: "playing", positionMs: 2500, durationMs: 10_000, error: null })).toBe(0.25);
    expect(progressFraction({ status: "playing", positionMs: 99_999, durationMs: 10_000, error: null })).toBe(1);
  });
});

describe("seekTargetMs", () => {
  it("maps a 0..1 fraction to a clamped millisecond offset", () => {
    expect(seekTargetMs(0.5, 10_000)).toBe(5000);
    expect(seekTargetMs(-1, 10_000)).toBe(0);
    expect(seekTargetMs(2, 10_000)).toBe(10_000);
  });

  it("is 0 when the duration is unknown", () => {
    expect(seekTargetMs(0.5, 0)).toBe(0);
  });
});
