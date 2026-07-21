import { beforeEach, describe, expect, it, vi } from "vitest";

import { browserAudioSource } from "../src/browserAudio";
import { decodeBase64 } from "../src/pcm";

// jsdom has no Web Audio API; capture the worklet handler the source installs so the
// test can fire a synthetic audio frame.
let lastProcessor: { onaudioprocess: ((e: unknown) => void) | null };

class FakeNode {
  connect = vi.fn();
  disconnect = vi.fn();
}
class FakeProcessor extends FakeNode {
  onaudioprocess: ((e: unknown) => void) | null = null;
  constructor() {
    super();
    lastProcessor = this;
  }
}
class FakeAudioContext {
  sampleRate = 48000;
  destination = {};
  createMediaStreamSource = vi.fn(() => new FakeNode());
  createScriptProcessor = vi.fn(() => new FakeProcessor());
  close = vi.fn(async () => {});
}

const fakeStream = () =>
  ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream;

beforeEach(() => {
  lastProcessor = { onaudioprocess: null };
  (globalThis as unknown as { AudioContext: unknown }).AudioContext = FakeAudioContext;
  (window as unknown as { isSecureContext: boolean }).isSecureContext = true;
});

describe("browserAudioSource", () => {
  it("returns false when mic permission is denied", async () => {
    (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
      getUserMedia: vi.fn().mockRejectedValue(new Error("denied")),
    };
    const src = browserAudioSource();
    expect(await src.requestPermission()).toBe(false);
  });

  it("reports a denial when getUserMedia rejects with NotAllowedError", async () => {
    const err = new Error("denied");
    err.name = "NotAllowedError";
    (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
      getUserMedia: vi.fn().mockRejectedValue(err),
    };
    const src = browserAudioSource();
    expect(await src.requestPermission()).toBe(false);
    expect(src.lastPermissionError).toMatch(/permission denied/i);
    expect(src.lastPermissionError).toMatch(/shields/i); // Brave-specific hint
  });

  it("reports a missing device for NotFoundError", async () => {
    const err = new Error("none");
    err.name = "NotFoundError";
    (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
      getUserMedia: vi.fn().mockRejectedValue(err),
    };
    const src = browserAudioSource();
    expect(await src.requestPermission()).toBe(false);
    expect(src.lastPermissionError).toMatch(/no microphone/i);
  });

  it("blames the insecure origin when the mic API is absent (http on mobile)", async () => {
    (navigator as unknown as { mediaDevices: unknown }).mediaDevices = undefined;
    (window as unknown as { isSecureContext: boolean }).isSecureContext = false;
    const src = browserAudioSource();
    expect(await src.requestPermission()).toBe(false);
    expect(src.lastPermissionError).toMatch(/https/i);
  });

  it("clears the last error once permission is granted", async () => {
    const err = new Error("denied");
    err.name = "NotAllowedError";
    const getUserMedia = vi
      .fn()
      .mockRejectedValueOnce(err)
      .mockResolvedValueOnce(fakeStream());
    (navigator as unknown as { mediaDevices: unknown }).mediaDevices = { getUserMedia };
    const src = browserAudioSource();
    expect(await src.requestPermission()).toBe(false);
    expect(src.lastPermissionError).toBeTruthy();
    expect(await src.requestPermission()).toBe(true);
    expect(src.lastPermissionError).toBeUndefined();
  });

  it("streams base64 s16le chunks from the worklet while running", async () => {
    (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
      getUserMedia: vi.fn().mockResolvedValue(fakeStream()),
    };
    const src = browserAudioSource();
    expect(await src.requestPermission()).toBe(true);

    const chunks: string[] = [];
    expect(await src.start((b) => chunks.push(b))).toBe(true);

    // One synthetic 48 kHz frame of 4096 samples.
    const input = new Float32Array(4096).fill(0.5);
    lastProcessor.onaudioprocess?.({ inputBuffer: { getChannelData: () => input } });

    expect(chunks).toHaveLength(1);
    const bytes = decodeBase64(chunks[0]);
    expect(bytes.length % 2).toBe(0); // s16le → even byte count
    expect(bytes.length).toBeGreaterThan(0); // 4096 @ 48k → ~1365 samples → ~2730 bytes

    await src.stop();
  });
});
