import type { CueLevel, Lang, MicSource } from "@tenir/contract";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiHandlers } from "../src/ws";
import type { PcmAudioSource } from "../src/pcmSource";
import {
  CaptureSession,
  CUE_TTL_MS,
  initialCaptureState,
  reduce,
  type CaptureSessionDeps,
  type CaptureState,
  type ApiLike,
} from "../src/captureSession";

// ---- pure reducer -----------------------------------------------------------

describe("reduce", () => {
  const base = (): CaptureState => ({ ...initialCaptureState("phone-microphone"), running: true });

  it("appends a final turn and clears the live partial", () => {
    let s = reduce(base(), { type: "partial", text: "hello wor" });
    expect(s.partial).toBe("hello wor");
    s = reduce(s, { type: "final", segmentId: "a", text: "hello world" });
    expect(s.segments).toEqual([{ id: "a", text: "hello world" }]);
    expect(s.partial).toBe("");
  });



  it("caps retained segments at the memory bound", () => {
    let s = base();
    for (let i = 0; i < 70; i++) s = reduce(s, { type: "final", segmentId: `s${i}`, text: `t${i}` });
    expect(s.segments.length).toBe(60);
    expect(s.segments[0].id).toBe("s10"); // oldest dropped
    expect(s.segments[s.segments.length - 1].id).toBe("s69");
  });

  it("toggles pause and drops the stale partial", () => {
    let s = reduce(base(), { type: "partial", text: "half a sen" });
    s = reduce(s, { type: "togglePause" });
    expect(s.listening).toBe(false);
    expect(s.partial).toBe("");
  });


  it("drops to idle on stop but keeps the transcript and clears live cues", () => {
    let s = reduce(base(), { type: "final", segmentId: "a", text: "hi" });
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "T", body: "B" } });
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "T2", body: "B2" } }); // queued
    s = reduce(s, { type: "stop" });
    expect(s.running).toBe(false);
    expect(s.connection).toBe("closed");
    expect(s.segments.length).toBe(1); // transcript stays on screen to read back
    expect(s.activeCue).toBeNull(); // active cue cleared
    expect(s.queuedCues).toEqual([]); // and the whole backlog with it
  });

  it("shows the first cue and queues the rest behind it (XERK-102)", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "Sun", body: "150M" } });
    expect(s.activeCue).toEqual({ id: "c1", title: "Sun", body: "150M" });
    expect(s.queuedCues).toEqual([]);
    // A second cue while the first is up waits its turn rather than clobbering it.
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "Moon", body: "384k" } });
    expect(s.activeCue?.id).toBe("c1");
    expect(s.queuedCues.map((c) => c.id)).toEqual(["c2"]);
    // A third stacks behind the second (FIFO).
    s = reduce(s, { type: "cue", cue: { id: "c3", title: "Mars", body: "225M" } });
    expect(s.queuedCues.map((c) => c.id)).toEqual(["c2", "c3"]);
  });

  it("de-duplicates a re-delivered cue by id in place, active or queued", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "Sun", body: "150M" } });
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "Moon", body: "384k" } }); // queued
    // Same id as the active cue updates it in place, not a duplicate.
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "Sun", body: "updated" } });
    expect(s.activeCue).toEqual({ id: "c1", title: "Sun", body: "updated" });
    expect(s.queuedCues.map((c) => c.id)).toEqual(["c2"]);
    // Same id as a queued cue updates that slot, keeping its place in line.
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "Moon", body: "closer" } });
    expect(s.queuedCues).toEqual([{ id: "c2", title: "Moon", body: "closer" }]);
  });

  it("caps the backlog, dropping the stalest waiting cue", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "active", title: "t", body: "b" } });
    // 16 more pile up behind the active one; the queue holds at most 16.
    for (let i = 1; i <= 20; i++) s = reduce(s, { type: "cue", cue: { id: `q${i}`, title: "t", body: "b" } });
    expect(s.activeCue?.id).toBe("active");
    expect(s.queuedCues.length).toBe(16);
    // The oldest waiting cues (q1..q4) fell off; the freshest survive, in order.
    expect(s.queuedCues[0].id).toBe("q5");
    expect(s.queuedCues[s.queuedCues.length - 1].id).toBe("q20");
  });

  it("releases the active cue and promotes the queue head (XERK-102)", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "T", body: "B" } });
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "T2", body: "B2" } }); // queued
    s = reduce(s, { type: "cueRelease", id: "c1" });
    expect(s.activeCue?.id).toBe("c2"); // next in line pops immediately
    expect(s.queuedCues).toEqual([]);
    // Releasing the last cue clears the surface.
    s = reduce(s, { type: "cueRelease", id: "c2" });
    expect(s.activeCue).toBeNull();
    // A stale release (wrong / already-gone id) is a no-op (same reference back).
    const same = reduce(s, { type: "cueRelease", id: "ghost" });
    expect(same).toBe(s);
  });
});

// ---- session controller -----------------------------------------------------

class FakeApi implements ApiLike {
  started: {
    params: { micSource: MicSource; sourceLang?: Lang; cueLevel?: CueLevel };
    resume?: string;
  }[] = [];
  audio: Uint8Array[] = [];
  micSwitches: MicSource[] = [];
  stopped = false;
  constructor(readonly handlers: ApiHandlers) {}
  start(
    params: { micSource: MicSource; sourceLang?: Lang; cueLevel?: CueLevel },
    resumeSessionId?: string,
  ): void {
    this.started.push({ params, resume: resumeSessionId });
  }
  stop(): void {
    this.stopped = true;
  }
  sendAudio(pcm: Uint8Array): boolean {
    this.audio.push(pcm);
    return true;
  }
  switchMic(micSource: MicSource): void {
    this.micSwitches.push(micSource);
  }
}

class FakeAudio implements PcmAudioSource {
  granted = true;
  startOk = true;
  stopped = false;
  lastPermissionError?: string;
  onChunk: ((b: string) => void) | null = null;
  async requestPermission(): Promise<boolean> {
    return this.granted;
  }
  async start(onChunk: (b: string) => void): Promise<boolean> {
    this.onChunk = onChunk;
    return this.startOk;
  }
  async stop(): Promise<void> {
    this.stopped = true;
  }
}

function harness(resume: string | null = null, cueLevel?: CueLevel) {
  const audio = new FakeAudio();
  const refs: { client: FakeApi | null; saved: string | null; cleared: boolean } = {
    client: null,
    saved: null,
    cleared: false,
  };
  const deps: CaptureSessionDeps = {
    createClient: (handlers) => (refs.client = new FakeApi(handlers)),
    audio,
    loadSessionId: async () => resume,
    saveSessionId: (id) => {
      refs.saved = id;
    },
    clearSessionId: () => {
      refs.cleared = true;
    },
    defaultMicSource: "phone-microphone",
    cueLevel,
  };
  return { session: new CaptureSession(deps), audio, refs };
}

const cue = (id: string, title = "Sun", body = "150M km") =>
  ({ type: "cue" as const, cueId: id, title, body, atMs: 1000 });

const ready = (sessionId: string) => ({ type: "session.ready" as const, sessionId });

describe("CaptureSession", () => {
  it("requests permission, opens the api, and starts the mic", async () => {
    const { session, audio, refs } = harness();
    const ok = await session.start();
    expect(ok).toBe(true);
    expect(audio.onChunk).toBeTypeOf("function");
    expect(refs.client?.started).toEqual([{ params: { micSource: "phone-microphone", sourceLang: undefined }, resume: undefined }]);
    expect(session.getState().running).toBe(true);
  });

  it("refuses to start (with an error) when the mic permission is denied", async () => {
    const { session, audio } = harness();
    audio.granted = false;
    expect(await session.start()).toBe(false);
    expect(session.getState().running).toBe(false);
    expect(session.getState().error).toMatch(/permission/i);
  });

  it("surfaces the source's specific permission error when present", async () => {
    const { session, audio } = harness();
    audio.granted = false;
    audio.lastPermissionError = "Microphone access needs a secure (HTTPS) connection.";
    expect(await session.start()).toBe(false);
    expect(session.getState().error).toBe("Microphone access needs a secure (HTTPS) connection.");
  });

  it("persists the authoritative session id from session.ready", async () => {
    const { session, refs } = harness();
    await session.start();
    refs.client!.handlers.onReady?.(ready("auth-1"));
    expect(refs.saved).toBe("auth-1");
    expect(session.getState().sessionId).toBe("auth-1");
  });

  it("resumes a persisted session id on start", async () => {
    const { session, refs } = harness("prior-9");
    await session.start();
    expect(refs.client?.started[0].resume).toBe("prior-9");
    expect(session.getState().sessionId).toBe("prior-9"); // shown as resumed before ready
  });

  it("uploads decoded PCM while listening and drops it while paused", async () => {
    const { session, audio, refs } = harness();
    await session.start();
    const chunk = Buffer.from([1, 2, 3, 4]).toString("base64");

    audio.onChunk!(chunk);
    expect(refs.client?.audio).toHaveLength(1);
    expect(Array.from(refs.client!.audio[0])).toEqual([1, 2, 3, 4]);

    session.togglePause();
    audio.onChunk!(chunk);
    expect(refs.client?.audio).toHaveLength(1); // dropped while paused
  });

  it("forwards a runtime mic switch to the api and state", async () => {
    const { session, refs } = harness();
    await session.start();
    session.switchMic("g2-microphone");
    expect(refs.client?.micSwitches).toEqual(["g2-microphone"]);
    expect(session.getState().micSource).toBe("g2-microphone");
  });

  it("tears down the mic, socket, and resume id on stop", async () => {
    const { session, audio, refs } = harness();
    await session.start();
    await session.stop();
    expect(audio.stopped).toBe(true);
    expect(refs.client?.stopped).toBe(true);
    expect(refs.cleared).toBe(true);
    expect(session.getState().running).toBe(false);
  });

  it("notifies subscribers of state changes", async () => {
    const { session, refs } = harness();
    const seen: boolean[] = [];
    session.subscribe((s) => seen.push(s.running));
    expect(seen).toEqual([false]); // immediate current-state emit
    await session.start();
    refs.client!.handlers.onReady?.(ready("x"));
    expect(seen[seen.length - 1]).toBe(true);
  });

  it("forwards the chosen cue level to the api on start", async () => {
    const { session, refs } = harness(null, "aggressive");
    await session.start();
    expect(refs.client?.started[0].params.cueLevel).toBe("aggressive");
  });

  describe("live cues", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("shows an incoming cue then releases it after the TTL", async () => {
      const { session, refs } = harness();
      await session.start();
      refs.client!.handlers.onCue?.(cue("c1", "Sun", "About 150M km"));
      expect(session.getState().activeCue).toEqual({ id: "c1", title: "Sun", body: "About 150M km" });

      vi.advanceTimersByTime(CUE_TTL_MS - 1);
      expect(session.getState().activeCue).not.toBeNull(); // still visible just before TTL
      vi.advanceTimersByTime(1);
      expect(session.getState().activeCue).toBeNull(); // released at TTL
    });

    it("queues cues and pops the next one the moment the active is released (XERK-102)", async () => {
      const { session, refs } = harness();
      await session.start();
      // Three cues arrive back to back; only the first shows, the rest queue.
      refs.client!.handlers.onCue?.(cue("c1", "Sun", "150M"));
      refs.client!.handlers.onCue?.(cue("c2", "Moon", "384k"));
      refs.client!.handlers.onCue?.(cue("c3", "Mars", "225M"));
      expect(session.getState().activeCue?.id).toBe("c1");
      expect(session.getState().queuedCues.map((c) => c.id)).toEqual(["c2", "c3"]);

      // First TTL: c2 takes over immediately with its own fresh countdown.
      vi.advanceTimersByTime(CUE_TTL_MS);
      expect(session.getState().activeCue?.id).toBe("c2");
      expect(session.getState().queuedCues.map((c) => c.id)).toEqual(["c3"]);

      // c2's countdown is its own full TTL, not a leftover from c1.
      vi.advanceTimersByTime(CUE_TTL_MS - 1);
      expect(session.getState().activeCue?.id).toBe("c2");
      vi.advanceTimersByTime(1);
      expect(session.getState().activeCue?.id).toBe("c3");

      // Last one drains the queue empty.
      vi.advanceTimersByTime(CUE_TTL_MS);
      expect(session.getState().activeCue).toBeNull();
      expect(session.getState().queuedCues).toEqual([]);
    });

    it("cancels the pending cue-release timer and clears the queue on stop", async () => {
      const { session, refs } = harness();
      await session.start();
      refs.client!.handlers.onCue?.(cue("c1"));
      refs.client!.handlers.onCue?.(cue("c2")); // queued behind c1
      await session.stop();
      expect(session.getState().activeCue).toBeNull(); // cleared by stop
      expect(session.getState().queuedCues).toEqual([]); // backlog cleared too
      // The pending timer must not resurrect or error after teardown.
      vi.advanceTimersByTime(CUE_TTL_MS * 2);
      expect(session.getState().activeCue).toBeNull();
      expect(session.getState().queuedCues).toEqual([]);
    });
  });
});
