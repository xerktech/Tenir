import type { Lang, MicSource } from "@tenir/contract";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiHandlers } from "../src/ws";
import type { PcmAudioSource } from "../src/pcmSource";
import {
  CaptureSession,
  CUE_TTL_MS,
  cueCountdownLabel,
  cueSecondsLeft,
  cueSecondsUntil,
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

  it("carries the detected language on a final turn (XERK-160)", () => {
    const s = reduce(base(), { type: "final", segmentId: "a", text: "hola", lang: "es" });
    expect(s.segments[0].lang).toBe("es");
  });

  it("attaches a translation to its turn by segment id (XERK-160)", () => {
    let s = reduce(base(), { type: "final", segmentId: "a", text: "hola", lang: "es" });
    s = reduce(s, { type: "final", segmentId: "b", text: "adiós", lang: "es" });
    s = reduce(s, { type: "translation", segmentId: "a", text: "hello" });
    expect(s.segments[0].translation).toBe("hello");
    // Only the paired turn gains the translation.
    expect(s.segments[1].translation).toBeUndefined();
  });

  it("ignores a translation for a turn that is not in view (XERK-160)", () => {
    const s = base();
    expect(reduce(s, { type: "translation", segmentId: "ghost", text: "hello" })).toBe(s);
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
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "T", body: "B" }, now: 0 });
    s = reduce(s, { type: "cueTick", now: CUE_TTL_MS });
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
      now: 0,
    });
    expect(s.activeCue?.source).toBe("BBC News");
    // Released into the transcript once its window closes, the attribution rides along.
    s = reduce(s, { type: "cueTick", now: CUE_TTL_MS });
    expect(s.pastCues[0].source).toBe("BBC News");
  });

  it("drops to idle on stop but keeps the transcript and clears live cues", () => {
    let s = reduce(base(), { type: "final", segmentId: "a", text: "hi" });
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "T", body: "B" }, now: 0 });
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "T2", body: "B2" }, now: 0 }); // queued
    s = reduce(s, { type: "stop" });
    expect(s.running).toBe(false);
    expect(s.connection).toBe("closed");
    expect(s.segments.length).toBe(1); // transcript stays on screen to read back
    expect(s.activeCue).toBeNull(); // active cue cleared
    expect(s.queuedCues).toEqual([]); // and the whole backlog with it
    expect(s.activeCueEndsAt).toBeNull(); // the schedule is torn down too
  });

  it("shows the first cue and queues the rest behind it (XERK-102)", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "Sun", body: "150M" }, now: 0 });
    // No turns yet → anchors before the transcript; its window ends one TTL out.
    expect(s.activeCue).toEqual({ id: "c1", title: "Sun", body: "150M", afterSegmentId: null });
    expect(s.activeCueEndsAt).toBe(CUE_TTL_MS);
    expect(s.queuedCues).toEqual([]);
    // A second cue arriving while the first is still inside its window waits its turn.
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "Moon", body: "384k" }, now: 1000 });
    expect(s.activeCue?.id).toBe("c1");
    expect(s.queuedCues.map((c) => c.id)).toEqual(["c2"]);
    // A third stacks behind the second (FIFO).
    s = reduce(s, { type: "cue", cue: { id: "c3", title: "Mars", body: "225M" }, now: 2000 });
    expect(s.queuedCues.map((c) => c.id)).toEqual(["c2", "c3"]);
  });

  it("de-duplicates a re-delivered cue by id in place, active or queued", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "Sun", body: "150M" }, now: 0 });
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "Moon", body: "384k" }, now: 1000 }); // queued
    // Same id as the active cue updates it in place, not a duplicate.
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "Sun", body: "updated" }, now: 2000 });
    expect(s.activeCue).toEqual({ id: "c1", title: "Sun", body: "updated", afterSegmentId: null });
    expect(s.queuedCues.map((c) => c.id)).toEqual(["c2"]);
    // Same id as a queued cue updates that slot, keeping its place in line.
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "Moon", body: "closer" }, now: 3000 });
    expect(s.queuedCues).toEqual([{ id: "c2", title: "Moon", body: "closer", afterSegmentId: null }]);
  });

  it("caps the backlog, dropping the stalest waiting cue", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "active", title: "t", body: "b" }, now: 0 });
    // 20 more arrive inside the active cue's window; the queue holds at most 16.
    for (let i = 1; i <= 20; i++) {
      s = reduce(s, { type: "cue", cue: { id: `q${i}`, title: "t", body: "b" }, now: 0 });
    }
    expect(s.activeCue?.id).toBe("active");
    expect(s.queuedCues.length).toBe(16);
    // The oldest waiting cues (q1..q4) fell off; the freshest survive, in order.
    expect(s.queuedCues[0].id).toBe("q5");
    expect(s.queuedCues[s.queuedCues.length - 1].id).toBe("q20");
  });

  it("releases the active cue and promotes the queue head as its window ends (XERK-102)", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "T", body: "B" }, now: 0 });
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "T2", body: "B2" }, now: 500 }); // queued
    // A tick at c1's window close retires it and promotes c2 for its own full turn.
    s = reduce(s, { type: "cueTick", now: CUE_TTL_MS });
    expect(s.activeCue?.id).toBe("c2");
    expect(s.activeCueEndsAt).toBe(CUE_TTL_MS * 2); // c2 gets a full window, not c1's remainder
    expect(s.queuedCues).toEqual([]);
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1"]);
    // c2's window closes a full TTL later, clearing the band.
    s = reduce(s, { type: "cueTick", now: CUE_TTL_MS * 2 });
    expect(s.activeCue).toBeNull();
    expect(s.activeCueEndsAt).toBeNull();
    // A tick with nothing due is a no-op (same reference back).
    const same = reduce(s, { type: "cueTick", now: CUE_TTL_MS * 5 });
    expect(same).toBe(s);
  });

  it("advances the queue on wall-clock time so a backgrounded gap doesn't replay (XERK-159)", () => {
    // Four cues arrive close together while the app is backgrounded — no ticks fire
    // (the release timer is frozen), so they pile onto the continuous schedule.
    let s = base();
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "t", body: "b" }, now: 0 });
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "t", body: "b" }, now: 1000 });
    s = reduce(s, { type: "cue", cue: { id: "c3", title: "t", body: "b" }, now: 2000 });
    s = reduce(s, { type: "cue", cue: { id: "c4", title: "t", body: "b" }, now: 3000 });
    expect(s.activeCue?.id).toBe("c1");
    expect(s.queuedCues.map((c) => c.id)).toEqual(["c2", "c3", "c4"]);

    // Coming back to the foreground 3.5 windows later, ONE tick reconciles the whole
    // schedule: c1 [0,T], c2 [T,2T] and c3 [2T,3T] have all closed → into the
    // transcript at once (no one-at-a-time replay); c4's window [3T,4T] is still
    // open, so it holds the band for the time it has left.
    s = reduce(s, { type: "cueTick", now: CUE_TTL_MS * 3 + 500 });
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1", "c2", "c3"]);
    expect(s.activeCue?.id).toBe("c4");
    expect(s.queuedCues).toEqual([]);
    expect(s.activeCueEndsAt).toBe(CUE_TTL_MS * 4);
  });

  it("clears the band when the whole queue's windows have closed while away (XERK-159)", () => {
    // Same backgrounded burst, but the wearer returns long after every window closed:
    // there is nothing left to show, so the band is empty and all cues are inline.
    let s = base();
    for (let i = 1; i <= 4; i++) {
      s = reduce(s, { type: "cue", cue: { id: `c${i}`, title: "t", body: "b" }, now: (i - 1) * 500 });
    }
    s = reduce(s, { type: "cueTick", now: CUE_TTL_MS * 10 });
    expect(s.activeCue).toBeNull();
    expect(s.activeCueEndsAt).toBeNull();
    expect(s.queuedCues).toEqual([]);
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1", "c2", "c3", "c4"]);
  });

  it("holds the band while the active cue is still inside its window (XERK-159)", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "T", body: "B" }, now: 0 });
    // A tick before the window closes must not churn the state.
    const early = reduce(s, { type: "cueTick", now: CUE_TTL_MS - 1 });
    expect(early).toBe(s);
    expect(early.activeCue?.id).toBe("c1");
  });

  // ---- past cues embedded in the live transcript (XERK-108) -----------------

  it("anchors a cue to the last turn and embeds it in the transcript on release", () => {
    let s = reduce(base(), { type: "final", segmentId: "a", text: "hello" });
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "Sun", body: "150M" }, now: 0 });
    // The cue is anchored to the turn that was showing when it arrived.
    expect(s.activeCue?.afterSegmentId).toBe("a");
    expect(s.pastCues).toEqual([]); // not in the transcript while it's still in the band
    // A later turn doesn't move an already-anchored cue.
    s = reduce(s, { type: "final", segmentId: "b", text: "world" });
    s = reduce(s, { type: "cueTick", now: CUE_TTL_MS });
    expect(s.activeCue).toBeNull();
    expect(s.pastCues).toEqual([{ id: "c1", title: "Sun", body: "150M", afterSegmentId: "a" }]);
  });

  it("keeps embedded past cues across a session stop, only clearing live band cues", () => {
    let s = reduce(base(), { type: "final", segmentId: "a", text: "hi" });
    s = reduce(s, { type: "cue", cue: { id: "c1", title: "T", body: "B" }, now: 0 });
    s = reduce(s, { type: "cueTick", now: CUE_TTL_MS }); // c1 → transcript
    s = reduce(s, { type: "cue", cue: { id: "c2", title: "T2", body: "B2" }, now: CUE_TTL_MS }); // still in band
    s = reduce(s, { type: "stop" });
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1"]); // reviewed cue stays with the transcript
    expect(s.activeCue).toBeNull(); // the still-live band cue is dropped
  });

  it("does not double a past cue if a released cue is re-delivered (stale timer / resume)", () => {
    let s = reduce(base(), { type: "cue", cue: { id: "c1", title: "T", body: "B" }, now: 0 });
    s = reduce(s, { type: "cueTick", now: CUE_TTL_MS });
    expect(s.pastCues.map((c) => c.id)).toEqual(["c1"]);
    // A resume re-delivers the already-reviewed cue: it must not re-enter the band.
    const after = reduce(s, { type: "cue", cue: { id: "c1", title: "T", body: "B" }, now: CUE_TTL_MS + 100 });
    expect(after).toBe(s); // no-op
    expect(after.activeCue).toBeNull();
    expect(after.pastCues.map((c) => c.id)).toEqual(["c1"]);
  });

  it("bounds the retained past cues", () => {
    let s = base();
    // Release 70 cues one after another, each its own full window; keep the last 60.
    for (let i = 0; i < 70; i++) {
      s = reduce(s, { type: "cue", cue: { id: `c${i}`, title: "t", body: "b" }, now: i * CUE_TTL_MS });
      s = reduce(s, { type: "cueTick", now: (i + 1) * CUE_TTL_MS });
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

describe("cueSecondsUntil (XERK-159)", () => {
  it("counts from the cue's wall-clock end time against the current clock", () => {
    const endsAt = 50_000;
    // A full window ahead reads the full count; each second closer ticks it down.
    expect(cueSecondsUntil(endsAt, endsAt - CUE_TTL_MS)).toBe(10);
    expect(cueSecondsUntil(endsAt, endsAt - CUE_TTL_MS + 1000)).toBe(9);
    expect(cueSecondsUntil(endsAt, endsAt - 1000)).toBe(1);
    expect(cueSecondsUntil(endsAt, endsAt)).toBe(0);
  });

  it("shows only the time a mid-window cue has left, not a fresh ten", () => {
    // A cue promoted on return whose window opened 7s ago (backdated schedule):
    // the count is truthful — 3s — rather than restarting at 10.
    const endsAt = 50_000;
    expect(cueSecondsUntil(endsAt, endsAt - 3000)).toBe(3);
  });

  it("clamps past a window that already closed or a clock that jumped back", () => {
    const endsAt = 50_000;
    expect(cueSecondsUntil(endsAt, endsAt + 5000)).toBe(0); // long overdue
    expect(cueSecondsUntil(endsAt, endsAt - CUE_TTL_MS * 3)).toBe(10); // never above the TTL
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

  it("attaches an incoming translation to its finalized turn (XERK-160)", async () => {
    const { session, refs } = harness();
    await session.start();
    refs.client!.handlers.onFinal?.({
      type: "caption.final",
      segmentId: "a",
      text: "hola",
      lang: "es",
      startMs: 0,
      endMs: 900,
    });
    refs.client!.handlers.onTranslation?.({ type: "translation", segmentId: "a", text: "hello" });
    expect(session.getState().segments[0]).toMatchObject({
      id: "a",
      text: "hola",
      lang: "es",
      translation: "hello",
    });
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

    it("shows queued cues in turn as each window closes on wall-clock time (XERK-102)", async () => {
      const { session, refs } = harness();
      await session.start();
      // Three cues arrive back to back within the first window: c1 shows, rest queue.
      refs.client!.handlers.onCue?.(cue("c1", "Sun", "150M"));
      refs.client!.handlers.onCue?.(cue("c2", "Moon", "384k"));
      refs.client!.handlers.onCue?.(cue("c3", "Mars", "225M"));
      expect(session.getState().activeCue?.id).toBe("c1");
      expect(session.getState().queuedCues.map((c) => c.id)).toEqual(["c2", "c3"]);

      // Each cue gets a full window: c1 hands off to c2 at the TTL, with its own
      // fresh countdown, and c1 drops into the transcript.
      vi.advanceTimersByTime(CUE_TTL_MS);
      expect(session.getState().activeCue?.id).toBe("c2");
      expect(session.getState().queuedCues.map((c) => c.id)).toEqual(["c3"]);
      expect(session.getState().pastCues.map((c) => c.id)).toEqual(["c1"]);

      // c2 shows for its own full TTL before c3 takes over.
      vi.advanceTimersByTime(CUE_TTL_MS - 1);
      expect(session.getState().activeCue?.id).toBe("c2");
      vi.advanceTimersByTime(1);
      expect(session.getState().activeCue?.id).toBe("c3");

      // The last one drains the band empty.
      vi.advanceTimersByTime(CUE_TTL_MS);
      expect(session.getState().activeCue).toBeNull();
      expect(session.getState().pastCues.map((c) => c.id)).toEqual(["c1", "c2", "c3"]);
    });

    it("drains cues whose turn passed while backgrounded when the app returns (XERK-159)", async () => {
      const { session, refs } = harness();
      await session.start();
      const t0 = Date.now();
      // A burst of cues arrives, then the app is backgrounded: the release timer
      // is frozen, so nothing drains while time passes.
      refs.client!.handlers.onCue?.(cue("c1"));
      refs.client!.handlers.onCue?.(cue("c2"));
      refs.client!.handlers.onCue?.(cue("c3"));
      expect(session.getState().activeCue?.id).toBe("c1");
      expect(session.getState().queuedCues.map((c) => c.id)).toEqual(["c2", "c3"]);

      // Jump the wall clock past two windows WITHOUT firing timers (backgrounded).
      vi.setSystemTime(t0 + CUE_TTL_MS * 2 + 500);
      // Returning to the foreground reconciles the schedule in one pass: c1 and c2
      // closed → transcript; c3 is mid-window and holds the band for the time it
      // has left — no one-at-a-time replay of the ones already gone.
      session.syncCues();
      expect(session.getState().pastCues.map((c) => c.id)).toEqual(["c1", "c2"]);
      expect(session.getState().activeCue?.id).toBe("c3");
      expect(session.getState().queuedCues).toEqual([]);

      // c3 finishes its remaining time on the re-armed real timer and clears.
      vi.advanceTimersByTime(CUE_TTL_MS);
      expect(session.getState().activeCue).toBeNull();
      expect(session.getState().pastCues.map((c) => c.id)).toEqual(["c1", "c2", "c3"]);
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
