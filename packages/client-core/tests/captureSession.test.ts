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
    s = reduce(s, { type: "stop" });
    expect(s.running).toBe(false);
    expect(s.connection).toBe("closed");
    expect(s.segments.length).toBe(1); // transcript stays on screen to read back
    expect(s.cues).toEqual([]); // ephemeral cues cleared
  });

  it("appends live cues, de-duplicates by id, and caps the visible set", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "Sun", body: "150M" } });
    expect(s.cues).toEqual([{ id: "c1", title: "Sun", body: "150M" }]);
    // Same id replaces in place rather than duplicating.
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "Sun", body: "updated" } });
    expect(s.cues).toEqual([{ id: "c1", title: "Sun", body: "updated" }]);
    // Overflow drops the oldest (visible cap is 4).
    for (let i = 2; i <= 6; i++) s = reduce(s, { type: "cue", cue: { id: `c${i}`, title: "t", body: "b" } });
    expect(s.cues.length).toBe(4);
    expect(s.cues[s.cues.length - 1].id).toBe("c6");
  });

  it("expires a cue by id", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "T", body: "B" } });
    s = reduce(s, { type: "cueExpire", id: "c1" });
    expect(s.cues).toEqual([]);
    // Expiring an unknown id is a no-op (same reference back).
    const same = reduce(s, { type: "cueExpire", id: "ghost" });
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

    it("shows an incoming cue then auto-dismisses it after the TTL", async () => {
      const { session, refs } = harness();
      await session.start();
      refs.client!.handlers.onCue?.(cue("c1", "Sun", "About 150M km"));
      expect(session.getState().cues).toEqual([{ id: "c1", title: "Sun", body: "About 150M km" }]);

      vi.advanceTimersByTime(CUE_TTL_MS - 1);
      expect(session.getState().cues).toHaveLength(1); // still visible just before TTL
      vi.advanceTimersByTime(1);
      expect(session.getState().cues).toEqual([]); // dismissed at TTL
    });

    it("cancels pending cue-dismiss timers on stop", async () => {
      const { session, refs } = harness();
      await session.start();
      refs.client!.handlers.onCue?.(cue("c1"));
      await session.stop();
      expect(session.getState().cues).toEqual([]); // cleared by stop
      // The pending timer must not resurrect or error after teardown.
      vi.advanceTimersByTime(CUE_TTL_MS * 2);
      expect(session.getState().cues).toEqual([]);
    });
  });
});
