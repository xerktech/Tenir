import { describe, expect, it } from "vitest";

import { decodeBase64, downsampleTo16k, encodeBase64, floatToPcm16 } from "../src/pcm";

// Encode with Node's Buffer so fixtures are independent of our own codec.
const b64 = (bytes: number[]) => Buffer.from(bytes).toString("base64");

describe("decodeBase64", () => {
  it("decodes the empty string to no bytes", () => {
    expect(decodeBase64("")).toEqual(new Uint8Array(0));
  });

  it("round-trips byte sequences of every padding length", () => {
    for (const len of [1, 2, 3, 4, 5, 100]) {
      const bytes = Array.from({ length: len }, (_, i) => (i * 37) & 0xff);
      expect(Array.from(decodeBase64(b64(bytes)))).toEqual(bytes);
    }
  });

  it("decodes full-range PCM bytes (0x00..0xff)", () => {
    const bytes = Array.from({ length: 256 }, (_, i) => i);
    expect(Array.from(decodeBase64(b64(bytes)))).toEqual(bytes);
  });

  it("ignores embedded whitespace/newlines", () => {
    const encoded = b64([1, 2, 3, 4, 5, 6]);
    const withWs = `${encoded.slice(0, 4)}\n ${encoded.slice(4)}`;
    expect(Array.from(decodeBase64(withWs))).toEqual([1, 2, 3, 4, 5, 6]);
  });
});

describe("encodeBase64", () => {
  it("round-trips through decodeBase64", () => {
    const bytes = new Uint8Array(Array.from({ length: 300 }, (_, i) => (i * 31) & 0xff));
    expect(Array.from(decodeBase64(encodeBase64(bytes)))).toEqual(Array.from(bytes));
  });
});

describe("floatToPcm16", () => {
  it("maps the float range to signed-16-bit little-endian bytes", () => {
    const out = floatToPcm16(new Float32Array([0, 1, -1]));
    expect(out.length).toBe(6); // 3 samples * 2 bytes
    const view = new DataView(out.buffer);
    expect(view.getInt16(0, true)).toBe(0);
    expect(view.getInt16(2, true)).toBe(0x7fff);
    expect(view.getInt16(4, true)).toBe(-0x8000);
  });
});

describe("downsampleTo16k", () => {
  it("returns the input unchanged when already at 16 kHz", () => {
    const samples = new Float32Array([0.1, 0.2, 0.3]);
    expect(downsampleTo16k(samples, 16000)).toBe(samples);
  });

  it("shrinks a 48 kHz buffer by ~3x", () => {
    const out = downsampleTo16k(new Float32Array(4800), 48000);
    expect(out.length).toBe(1600);
  });
});
