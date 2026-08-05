/**
 * Ported from upstream `packages/client-core/tests/ws.test.ts` (vitest →
 * bun:test). Bun has no timer faking, so scheduled reconnects are captured by
 * stubbing `setTimeout` and run by hand.
 */

import { afterEach, beforeEach, describe, expect, it, mock } from "bun:test";

import { clearToken, setToken } from "./auth";
import type { ServerMessage } from "./messages";
import { ApiClient } from "./ws";

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  binaryType = "";
  bufferedAmount = 0;
  sent: unknown[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    instances.push(this);
  }

  send(data: unknown): void {
    this.sent.push(data);
  }

  // Mirrors the DOM CloseEvent: a code accompanies every close (1000 normal,
  // 1006 abnormal drop, 1008 policy violation — the api's auth rejection).
  close(code = 1000): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code });
  }

  // ---- test helpers ----
  open(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  emit(msg: ServerMessage): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }

  jsonSent(): Record<string, unknown>[] {
    return this.sent.filter((s) => typeof s === "string").map((s) => JSON.parse(s as string));
  }

  lastJson(): Record<string, unknown> {
    const all = this.jsonSent();
    return all[all.length - 1];
  }
}

let instances: MockWebSocket[];
let scheduled: Array<{ fn: () => void; delay: number }>;
const realSetTimeout = globalThis.setTimeout;
const realWebSocket = globalThis.WebSocket;

/** Run every timer scheduled so far (a fired timer may schedule more). */
function runScheduled(): void {
  while (scheduled.length > 0) {
    const batch = scheduled;
    scheduled = [];
    for (const t of batch) t.fn();
  }
}

beforeEach(() => {
  instances = [];
  scheduled = [];
  clearToken();
  globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket;
  globalThis.setTimeout = ((fn: () => void, delay?: number) => {
    scheduled.push({ fn, delay: delay ?? 0 });
    return 0;
  }) as unknown as typeof setTimeout;
});

afterEach(() => {
  globalThis.setTimeout = realSetTimeout;
  globalThis.WebSocket = realWebSocket;
});

describe("ApiClient", () => {
  it("reports connection state and sends session.start on open", () => {
    const states: string[] = [];
    const client = new ApiClient("ws://h/ws", {
      onConnectionChange: (s) => states.push(s),
    });
    client.start({ micSource: "g2-microphone" });
    expect(states).toEqual(["connecting"]);

    instances[0].open();
    expect(states).toEqual(["connecting", "open"]);
    expect(instances[0].jsonSent()[0]).toEqual({
      type: "session.start",
      micSource: "g2-microphone",
      sourceLang: undefined,
    });
  });

  it("appends the bearer token to the WS URL as ?token=", () => {
    setToken("tok-123");
    const client = new ApiClient("ws://h/ws");
    client.start({ micSource: "g2-microphone" });
    expect(instances[0].url).toBe("ws://h/ws?token=tok-123");
  });

  it("resumes a prior session id in session.start", () => {
    const client = new ApiClient("ws://h/ws");
    client.start({ micSource: "phone-microphone" }, "sess-9");
    instances[0].open();
    expect(instances[0].jsonSent()[0]).toMatchObject({ sessionId: "sess-9" });
  });

  it("routes server messages to the matching handler and tracks the session id", () => {
    const onFinal = mock(() => {});
    const client = new ApiClient("ws://h/ws", { onFinal });
    client.start({ micSource: "g2-microphone" });
    instances[0].open();

    instances[0].emit({ type: "session.ready", sessionId: "abc" });
    expect(client.currentSessionId).toBe("abc");

    instances[0].emit({
      type: "caption.final",
      segmentId: "s1",
      text: "hello",
      startMs: 0,
      endMs: 1,
    });
    expect(onFinal).toHaveBeenCalledTimes(1);
  });

  it("routes cue / translation / song frames to their handlers", () => {
    const onCue = mock(() => {});
    const onTranslation = mock(() => {});
    const onTranslationDone = mock(() => {});
    const onSong = mock(() => {});
    const onSongSync = mock(() => {});
    const onSongDone = mock(() => {});
    const client = new ApiClient("ws://h/ws", {
      onCue,
      onTranslation,
      onTranslationDone,
      onSong,
      onSongSync,
      onSongDone,
    });
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    instances[0].emit({ type: "cue", cueId: "cue-1", title: "Sun", body: "150M km.", atMs: 1500 });
    instances[0].emit({ type: "translation", segmentId: "seg-1", text: "Hello", sourceLang: "es" });
    instances[0].emit({ type: "translation.done" });
    instances[0].emit({
      type: "song",
      songId: "sng-1",
      title: "Weird Fishes",
      artist: "Radiohead",
      atMs: 1000,
      offsetMs: 60000,
      lines: [{ atMs: 0, text: "a" }],
    });
    instances[0].emit({ type: "song.sync", songId: "sng-1", atMs: 2000, offsetMs: 78000 });
    instances[0].emit({ type: "song.done", songId: "sng-1" });
    expect(onCue.mock.calls[0][0]).toMatchObject({ cueId: "cue-1", title: "Sun" });
    expect(onTranslation.mock.calls[0][0]).toMatchObject({ segmentId: "seg-1", text: "Hello" });
    expect(onTranslationDone).toHaveBeenCalledTimes(1);
    expect(onSong.mock.calls[0][0]).toMatchObject({ songId: "sng-1", offsetMs: 60000 });
    expect(onSongSync.mock.calls[0][0]).toMatchObject({ songId: "sng-1", offsetMs: 78000 });
    expect(onSongDone.mock.calls[0][0]).toMatchObject({ songId: "sng-1" });
  });

  it("honours backpressure and socket state in sendAudio", () => {
    const client = new ApiClient("ws://h/ws");
    const pcm = new Uint8Array([1, 2, 3]);
    expect(client.sendAudio(pcm)).toBe(false); // not connected yet

    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    expect(client.sendAudio(pcm)).toBe(true);
    expect(instances[0].sent[instances[0].sent.length - 1]).toBe(pcm); // binary frame, not JSON

    instances[0].bufferedAmount = 1024 * 1024; // over the cap
    expect(client.sendAudio(pcm)).toBe(false);
  });

  it("switchMic updates params and sends mic.switch", () => {
    const client = new ApiClient("ws://h/ws");
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    client.switchMic("phone-microphone");
    expect(instances[0].lastJson()).toEqual({
      type: "mic.switch",
      micSource: "phone-microphone",
    });
  });

  it("stop ends the session, closes, and does not reconnect", () => {
    const client = new ApiClient("ws://h/ws");
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    client.stop();
    expect(instances[0].lastJson()).toEqual({ type: "session.end" });
    expect(instances[0].readyState).toBe(MockWebSocket.CLOSED);

    runScheduled(); // any scheduled reconnect would fire here
    expect(instances).toHaveLength(1); // no new socket opened
  });

  it("reconnects with exponential backoff after unexpected closes", () => {
    const client = new ApiClient("ws://h/ws");
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    instances[0].close(1006); // server dropped us (abnormal)
    expect(instances).toHaveLength(1);
    expect(scheduled).toHaveLength(1);
    expect(scheduled[0].delay).toBe(1000); // first backoff step: 1s

    scheduled.splice(0).forEach((t) => t.fn());
    expect(instances).toHaveLength(2);

    // A second drop before a successful open doubles the delay.
    instances[1].close(1006);
    expect(scheduled[0].delay).toBe(2000);
    scheduled.splice(0).forEach((t) => t.fn());
    instances[2].close(1006);
    expect(scheduled[0].delay).toBe(4000);

    // ...capped at 16s.
    for (let i = 0; i < 6; i++) {
      scheduled.splice(0).forEach((t) => t.fn());
      instances[instances.length - 1].close(1006);
    }
    expect(scheduled[0].delay).toBe(16000);

    // A successful open resets the backoff.
    scheduled.splice(0).forEach((t) => t.fn());
    instances[instances.length - 1].open();
    instances[instances.length - 1].close(1006);
    expect(scheduled[0].delay).toBe(1000);
  });

  it("re-sends session.start with the session id on reconnect", () => {
    const client = new ApiClient("ws://h/ws");
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    instances[0].emit({ type: "session.ready", sessionId: "sess-1" });
    instances[0].close(1006);
    runScheduled();
    instances[1].open();
    expect(instances[1].jsonSent()[0]).toMatchObject({
      type: "session.start",
      sessionId: "sess-1",
    });
  });

  it("does not reconnect after a 1008 policy close, and surfaces an auth error", () => {
    const onError = mock(() => {});
    const client = new ApiClient("ws://h/ws", { onError });
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    instances[0].close(1008); // api rejected the token / capture off
    expect(onError.mock.calls[0][0]).toMatchObject({ code: "unauthorized", fatal: true });
    runScheduled(); // no reconnect should be scheduled
    expect(instances).toHaveLength(1);
  });

  it("stops reconnecting after a fatal error message", () => {
    const onError = mock(() => {});
    const client = new ApiClient("ws://h/ws", { onError });
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    instances[0].emit({
      type: "error",
      code: "unauthorized",
      message: "recording is turned off",
      fatal: true,
    });
    expect(onError.mock.calls[0][0]).toMatchObject({ fatal: true });
    // The fatal error closes the socket; that close must not schedule a reconnect.
    runScheduled();
    expect(instances).toHaveLength(1);
  });
});
