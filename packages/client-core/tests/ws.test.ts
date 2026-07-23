import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ServerMessage } from "@tenir/contract";

import { clearToken } from "../src/auth";
import { ApiClient } from "../src/ws";

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

  // Mirrors the DOM CloseEvent: a code accompanies every close (1000 normal, 1006
  // abnormal drop, 1008 policy violation — the api's auth/capture rejection).
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

beforeEach(() => {
  instances = [];
  clearToken();
  vi.useFakeTimers();
  globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket;
});

afterEach(() => {
  vi.useRealTimers();
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

  it("resumes a prior session id in session.start", () => {
    const client = new ApiClient("ws://h/ws");
    client.start({ micSource: "phone-microphone" }, "sess-9");
    instances[0].open();
    expect(instances[0].jsonSent()[0]).toMatchObject({ sessionId: "sess-9" });
  });

  it("routes server messages to the matching handler and tracks the session id", () => {
    const onFinal = vi.fn();
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
    expect(onFinal).toHaveBeenCalledOnce();
  });

  it("routes a cue frame to onCue", () => {
    const onCue = vi.fn();
    const client = new ApiClient("ws://h/ws", { onCue });
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    instances[0].emit({
      type: "cue",
      cueId: "cue-1",
      title: "Sun",
      body: "About 150 million km away.",
      atMs: 1500,
    });
    expect(onCue).toHaveBeenCalledWith(
      expect.objectContaining({ cueId: "cue-1", title: "Sun", atMs: 1500 }),
    );
  });

  it("sends the chosen cue level in session.start", () => {
    const client = new ApiClient("ws://h/ws");
    client.start({ micSource: "g2-microphone", cueLevel: "conservative" });
    instances[0].open();
    expect(instances[0].jsonSent()[0]).toMatchObject({ cueLevel: "conservative" });
  });

  it("honours backpressure and socket state in sendAudio", () => {
    const client = new ApiClient("ws://h/ws");
    const pcm = new Uint8Array([1, 2, 3]);
    expect(client.sendAudio(pcm)).toBe(false); // not connected yet

    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    expect(client.sendAudio(pcm)).toBe(true);

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

    vi.runAllTimers(); // any scheduled reconnect would fire here
    expect(instances).toHaveLength(1); // no new socket opened
  });

  it("reconnects with backoff after an unexpected close", () => {
    const client = new ApiClient("ws://h/ws");
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    instances[0].close(1006); // server dropped us (abnormal)
    expect(instances).toHaveLength(1);
    vi.advanceTimersByTime(1000); // first backoff step
    expect(instances).toHaveLength(2);
  });

  it("does not reconnect after a 1008 policy close, and surfaces an auth error", () => {
    const onError = vi.fn();
    const client = new ApiClient("ws://h/ws", { onError });
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    instances[0].close(1008); // api rejected the token / capture off
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: "unauthorized", fatal: true }),
    );
    vi.runAllTimers(); // no reconnect should be scheduled
    expect(instances).toHaveLength(1);
  });

  it("stops reconnecting after a fatal error message", () => {
    const onError = vi.fn();
    const client = new ApiClient("ws://h/ws", { onError });
    client.start({ micSource: "g2-microphone" });
    instances[0].open();
    instances[0].emit({
      type: "error",
      code: "unauthorized",
      message: "recording is turned off",
      fatal: true,
    });
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ fatal: true }));
    // The fatal error closes the socket; that close must not schedule a reconnect.
    vi.runAllTimers();
    expect(instances).toHaveLength(1);
  });
});

