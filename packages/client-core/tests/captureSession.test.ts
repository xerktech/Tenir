import type { Lang, MicSource } from "@tenir/contract";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiHandlers } from "../src/ws";
import type { PcmAudioSource } from "../src/pcmSource";
import {
  CaptureSession,
  CUE_TTL_MS,
  cueCountdownLabel,
  cueSecondsLeft,
  initialCaptureState,
  liveTranscript,
  reduce,
  type CaptureSessionDeps,
  type CaptureState,
  type LiveCue,
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



  it("retains the whole live transcript so it never vanishes mid-session (XERK-135)", () => {
    let s = base();
    for (let i = 0; i < 500; i++) s = reduce(s, { type: "final", segmentId: `s${i}`, text: `t${i}` });
    // Every finalized turn is kept — the oldest is not dropped as the session
    // runs on, matching the history view. Previously a 60-turn cap silently
    // shifted off the earliest turns, so scrolling up in a long session showed
    // the lingering cues but no transcript.
    expect(s.segments.length).toBe(500);
    expect(s.segments[0].id).toBe("s0"); // oldest still present
    expect(s.segments[s.segments.length - 1].id).toBe("s499");
  });

  it("keeps every past cue anchored to its turn in a long transcript (XERK-135)", () => {
    // The symptom was cues surviving while their anchor turns were dropped, so
    // they floated to the top as leading cues. With the full transcript retained,
    // a cue released after the 100th turn still sits right after that turn.
    let s = base();
    for (let i = 0; i < 100; i++) s = reduce(s, { type: "final", segmentId: `s${i}`, text: `t${i}` });
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "T", body: "B" } });
    s = reduce(s, { type: "cueRelease", id: "c1" });
    for (let i = 100; i < 200; i++) s = reduce(s, { type: "final", segmentId: `s${i}`, text: `t${i}` });

    const items = liveTranscript(s.segments, s.pastCues);
    const cueIdx = items.findIndex((it) => it.kind === "cue");
    // The cue is inline after its anchor turn, not a leading cue at the very top.
    expect(cueIdx).toBeGreaterThan(0);
    const before = items[cueIdx - 1];
    expect(before.kind === "segment" && before.segment.id).toBe("s99");
  });

  it("toggles pause and drops the stale partial", () => {
    let s = reduce(base(), { type: "partial", text: "half a sen" });
    s = reduce(s, { type: "togglePause" });
    expect(s.listening).toBe(false);
    expect(s.partial).toBe("");
  });


  it("carries a grounded cue's source through the band into the reviewed transcript (XERK-120)", () => {
    let s = reduce(base(), { type: "final", segmentId: "a", text: "hi" });
    s = reduce(s, {
      type: "cue",
      cue: { id: "c1", title: "PM", body: "Andy Burnham took office.", source: "BBC News" },
    });
    expect(s.activeCue?.source).toBe("BBC News");
    // Released into the transcript, the attribution rides along for review.
    s = reduce(s, { type: "cueRelease", id: "c1" });
    expect(s.pastCues[0].source).toBe("BBC News");
  });

  it("drops to idle on stop but keeps the transcript and clears the live cue", () => {
    let s = reduce(base(), { type: "final", segmentId: "a", text: "hi" });
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "T", body: "B" } });
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "T2", body: "B2" } }); // supersedes c1
    s = reduce(s, { type: "stop" });
    expect(s.running).toBe(false);
    expect(s.connection).toBe("closed");
    expect(s.segments.length).toBe(1); // transcript stays on screen to read back
    expect(s.activeCue).toBeNull(); // the live band cue is cleared
    // c1 was superseded into the transcript before the stop, so it stays inline.
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1"]);
  });

  it("shows the newest cue and drops the ones it supersedes into the transcript (XERK-159)", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "Sun", body: "150M" } });
    // No turns yet, so the cue anchors before the transcript (afterSegmentId null).
    expect(s.activeCue).toEqual({ id: "c1", title: "Sun", body: "150M", afterSegmentId: null });
    expect(s.pastCues).toEqual([]);
    // A second cue takes the band immediately — the first drops straight into the
    // transcript rather than waiting in a queue, so you see the current cue live.
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "Moon", body: "384k" } });
    expect(s.activeCue?.id).toBe("c2");
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1"]);
    // A third supersedes the second the same way — always just the freshest up top.
    s = reduce(s, { type: "cue", cue: { id: "c3", title: "Mars", body: "225M" } });
    expect(s.activeCue?.id).toBe("c3");
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1", "c2"]);
  });

  it("de-duplicates a re-delivered cue by id in place while it holds the band", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "Sun", body: "150M" } });
    // Same id as the active cue updates it in place, not a duplicate — and it does
    // not push a copy of itself into the transcript.
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "Sun", body: "updated" } });
    expect(s.activeCue).toEqual({ id: "c1", title: "Sun", body: "updated", afterSegmentId: null });
    expect(s.pastCues).toEqual([]);
  });

  it("collapses a burst of cues to the freshest, the rest already inline (XERK-159)", () => {
    // The backgrounded-session case: a pile of cues lands in one go on return.
    // Only the newest sits in the band; every earlier one is already inline in
    // the transcript, where the wearer reads the ones they missed.
    let s = base();
    for (let i = 1; i <= 20; i++) s = reduce(s, { type: "cue", cue: { id: `c${i}`, title: "t", body: "b" } });
    expect(s.activeCue?.id).toBe("c20"); // the current cue, shown in real time
    // The 19 it superseded sit inline in order (bounded like every past-cue list).
    expect(s.pastCues.map((c) => c.id)).toEqual(
      Array.from({ length: 19 }, (_, i) => `c${i + 1}`),
    );
  });

  it("releases the active cue into the transcript and clears the band (XERK-159)", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "T", body: "B" } });
    s = reduce(s, { type: "cueRelease", id: "c1" });
    // Nothing waits behind it — the band clears and the cue is now inline.
    expect(s.activeCue).toBeNull();
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1"]);
    // A stale release (wrong / already-gone id) is a no-op (same reference back).
    const same = reduce(s, { type: "cueRelease", id: "ghost" });
    expect(same).toBe(s);
  });

  // ---- past cues embedded in the live transcript (XERK-108) -----------------

  it("anchors a cue to the last turn and embeds it in the transcript on release", () => {
    let s = reduce(base(), { type: "final", segmentId: "a", text: "hello" });
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "Sun", body: "150M" } });
    // The cue is anchored to the turn that was showing when it arrived.
    expect(s.activeCue?.afterSegmentId).toBe("a");
    expect(s.pastCues).toEqual([]); // not in the transcript while it's still in the band
    // A later turn doesn't move an already-anchored cue.
    s = reduce(s, { type: "final", segmentId: "b", text: "world" });
    s = reduce(s, { type: "cueRelease", id: "c1" });
    expect(s.activeCue).toBeNull();
    expect(s.pastCues).toEqual([{ id: "c1", title: "Sun", body: "150M", afterSegmentId: "a" }]);
  });

  it("keeps embedded past cues across a session stop, only clearing live band cues", () => {
    let s = reduce(base(), { type: "final", segmentId: "a", text: "hi" });
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "T", body: "B" } });
    s = reduce(s, { type: "cueRelease", id: "c1" }); // c1 → transcript
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "T2", body: "B2" } }); // still in band
    s = reduce(s, { type: "stop" });
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1"]); // reviewed cue stays with the transcript
    expect(s.activeCue).toBeNull(); // the still-live band cue is dropped
  });

  it("does not double a past cue if its release is re-applied (stale timer / resume)", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "T", body: "B" } });
    s = reduce(s, { type: "cueRelease", id: "c1" });
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1"]);
    // A resume re-delivers the already-reviewed cue: it must not re-enter the band.
    const after = reduce(s, { type: "cue", cue: { id: "c1", title: "T", body: "B" } });
    expect(after).toBe(s); // no-op
    expect(after.activeCue).toBeNull();
    expect(after.pastCues.map((c) => c.id)).toEqual(["c1"]);
  });

  it("bounds the retained past cues", () => {
    let s = base();
    // Release 70 cues one after another; only the last 60 are kept.
    for (let i = 0; i < 70; i++) {
      s = reduce(s, { type: "cue", cue: { id: `c${i}`, title: "t", body: "b" } });
      s = reduce(s, { type: "cueRelease", id: `c${i}` });
    }
    expect(s.pastCues.length).toBe(60);
    expect(s.pastCues[0].id).toBe("c10"); // oldest dropped
    expect(s.pastCues[s.pastCues.length - 1].id).toBe("c69");
  });
});

// ---- the live cue countdown (XERK-110) -------------------------------------

describe("cueSecondsLeft", () => {
  it("counts the whole seconds a cue still has on screen", () => {
    // The full count the instant it lands, and each boundary counts as the
    // second about to elapse — 9.001s left still reads as 10.
    expect(cueSecondsLeft(0)).toBe(10);
    expect(cueSecondsLeft(999)).toBe(10);
    expect(cueSecondsLeft(1000)).toBe(9);
    expect(cueSecondsLeft(1001)).toBe(9);
    expect(cueSecondsLeft(CUE_TTL_MS - 1000)).toBe(1);
    // 1 through the whole final second; 0 only once the cue is actually gone.
    expect(cueSecondsLeft(CUE_TTL_MS - 1)).toBe(1);
    expect(cueSecondsLeft(CUE_TTL_MS)).toBe(0);
  });

  it("clamps to the range the cue ever had", () => {
    // A late timer or a clock that jumped can't paint a negative count…
    expect(cueSecondsLeft(CUE_TTL_MS * 3)).toBe(0);
    // …nor one above the TTL it started from.
    expect(cueSecondsLeft(-5000)).toBe(10);
  });

  it("honours a caller's own TTL", () => {
    expect(cueSecondsLeft(0, 4000)).toBe(4);
    expect(cueSecondsLeft(2500, 4000)).toBe(2);
    expect(cueSecondsLeft(4000, 4000)).toBe(0);
  });
});

describe("cueCountdownLabel", () => {
  it("renders the count the way every cue surface paints it", () => {
    expect(cueCountdownLabel(10)).toBe("10s");
    expect(cueCountdownLabel(1)).toBe("1s");
    expect(cueCountdownLabel(0)).toBe("0s");
  });
});

// ---- liveTranscript: interleave turns and reviewed cues (XERK-108) ----------

describe("liveTranscript", () => {
  const seg = (id: string, text = id) => ({ id, text });
  const pastCue = (id: string, afterSegmentId: string | null): LiveCue => ({
    id,
    title: id,
    body: `${id}-body`,
    afterSegmentId,
  });

  it("places each past cue right after the turn it was anchored to", () => {
    const items = liveTranscript(
      [seg("a"), seg("b")],
      [pastCue("c1", "a"), pastCue("c2", "b")],
    );
    expect(items.map((i) => (i.kind === "segment" ? i.segment.id : `cue:${i.cue.id}`))).toEqual([
      "a",
      "cue:c1",
      "b",
      "cue:c2",
    ]);
  });

  it("leads with cues anchored before the transcript (null) or to a scrolled-off turn", () => {
    const items = liveTranscript(
      [seg("b")],
      [pastCue("c0", null), pastCue("c-gone", "a" /* dropped from the window */), pastCue("c1", "b")],
    );
    expect(items.map((i) => (i.kind === "segment" ? i.segment.id : `cue:${i.cue.id}`))).toEqual([
      "cue:c0",
      "cue:c-gone",
      "b",
      "cue:c1",
    ]);
  });

  it("keeps release order for multiple cues sharing one anchor", () => {
    const items = liveTranscript([seg("a")], [pastCue("c1", "a"), pastCue("c2", "a")]);
    expect(items.map((i) => (i.kind === "segment" ? i.segment.id : `cue:${i.cue.id}`))).toEqual([
      "a",
      "cue:c1",
      "cue:c2",
    ]);
  });
});

// ---- session controller -----------------------------------------------------

class FakeApi implements ApiLike {
  started: {
    params: { micSource: MicSource; sourceLang?: Lang };
    resume?: string;
  }[] = [];
  audio: Uint8Array[] = [];
  micSwitches: MicSource[] = [];
  stopped = false;
  constructor(readonly handlers: ApiHandlers) {}
  start(params: { micSource: MicSource; sourceLang?: Lang }, resumeSessionId?: string): void {
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

function harness(resume: string | null = null) {
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

  it("does not forward a cueLevel to the api on start (XERK-114)", async () => {
    const { session, refs } = harness();
    await session.start();
    expect(refs.client?.started[0].params).not.toHaveProperty("cueLevel");
  });

  describe("live cues", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("shows an incoming cue then releases it after the TTL", async () => {
      const { session, refs } = harness();
      await session.start();
      refs.client!.handlers.onCue?.(cue("c1", "Sun", "About 150M km"));
      expect(session.getState().activeCue).toEqual({
        id: "c1",
        title: "Sun",
        body: "About 150M km",
        afterSegmentId: null,
      });

      vi.advanceTimersByTime(CUE_TTL_MS - 1);
      expect(session.getState().activeCue).not.toBeNull(); // still visible just before TTL
      vi.advanceTimersByTime(1);
      expect(session.getState().activeCue).toBeNull(); // released at TTL
    });

    it("shows the freshest cue in real time, the rest already inline (XERK-159)", async () => {
      const { session, refs } = harness();
      await session.start();
      // Three cues arrive back to back (as when returning to a backgrounded
      // session): only the newest holds the band, the earlier two are already
      // inline in the transcript — no one-at-a-time replay of stale cues.
      refs.client!.handlers.onCue?.(cue("c1", "Sun", "150M"));
      refs.client!.handlers.onCue?.(cue("c2", "Moon", "384k"));
      refs.client!.handlers.onCue?.(cue("c3", "Mars", "225M"));
      expect(session.getState().activeCue?.id).toBe("c3");
      expect(session.getState().pastCues.map((c) => c.id)).toEqual(["c1", "c2"]);

      // c3's countdown is its own full TTL, not a leftover from an earlier cue.
      vi.advanceTimersByTime(CUE_TTL_MS - 1);
      expect(session.getState().activeCue?.id).toBe("c3");
      vi.advanceTimersByTime(1);
      // At the TTL it releases into the transcript and the band clears — nothing
      // is queued to pop up behind it.
      expect(session.getState().activeCue).toBeNull();
      expect(session.getState().pastCues.map((c) => c.id)).toEqual(["c1", "c2", "c3"]);
    });

    it("cancels the pending cue-release timer on stop", async () => {
      const { session, refs } = harness();
      await session.start();
      refs.client!.handlers.onCue?.(cue("c1"));
      refs.client!.handlers.onCue?.(cue("c2")); // supersedes c1 into the transcript
      await session.stop();
      expect(session.getState().activeCue).toBeNull(); // live band cue cleared
      expect(session.getState().pastCues.map((c) => c.id)).toEqual(["c1"]);
      // The pending timer must not resurrect or error after teardown.
      vi.advanceTimersByTime(CUE_TTL_MS * 2);
      expect(session.getState().activeCue).toBeNull();
    });
  });
});
