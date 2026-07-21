/**
 * Shared PCM/base64 helpers for browser audio capture (master plan §4.1, §10).
 *
 * The api STT wants 16 kHz s16le mono PCM, sent as raw binary WS frames. Browser
 * mics deliver float samples at the device rate (often 48 kHz); the mobile native
 * bridge marshals PCM as base64 strings. These dependency-free helpers bridge both
 * directions and are shared by the enrolment one-shot recorder (`record.ts`), the
 * streaming `browserAudioSource`, and the `CaptureSession`.
 */

/** Concatenate Float32 chunks into one buffer. */
export function concatFloat32(chunks: Float32Array[]): Float32Array {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.length;
  }
  return out;
}

/** Linear-interpolation resample from `inRate` to 16 kHz. */
export function downsampleTo16k(samples: Float32Array, inRate: number): Float32Array {
  const outRate = 16000;
  if (inRate === outRate) return samples;
  const ratio = inRate / outRate;
  const outLen = Math.floor(samples.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const a = Math.floor(pos);
    const b = Math.min(a + 1, samples.length - 1);
    out[i] = samples[a] + (samples[b] - samples[a]) * (pos - a);
  }
  return out;
}

/** Convert float [-1,1] samples to signed-16-bit little-endian bytes. */
export function floatToPcm16(samples: Float32Array): Uint8Array {
  const out = new Uint8Array(samples.length * 2);
  const view = new DataView(out.buffer);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return out;
}

/** Base64-encode bytes (chunked to dodge String.fromCharCode arg-count limits). */
export function encodeBase64(bytes: Uint8Array): string {
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

const B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

// Reverse lookup: ASCII code -> 6-bit value. Built once at module load.
const B64_LOOKUP = (() => {
  const table = new Int16Array(256).fill(-1);
  for (let i = 0; i < B64_ALPHABET.length; i++) table[B64_ALPHABET.charCodeAt(i)] = i;
  return table;
})();

/**
 * Decode a standard base64 string into raw bytes. Ignores whitespace and honours
 * `=` padding; unknown characters are skipped. Returns empty for empty input so a
 * stray empty chunk is a harmless no-op upstream. Dependency-free (React Native has
 * no reliable global `atob`).
 */
export function decodeBase64(b64: string): Uint8Array {
  let chars = 0;
  for (let i = 0; i < b64.length; i++) {
    if (B64_LOOKUP[b64.charCodeAt(i)] !== -1) chars++;
  }
  const out = new Uint8Array((chars * 3) >> 2);

  let acc = 0;
  let bits = 0;
  let o = 0;
  for (let i = 0; i < b64.length; i++) {
    const v = B64_LOOKUP[b64.charCodeAt(i)];
    if (v === -1) continue; // padding or whitespace
    acc = (acc << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[o++] = (acc >> bits) & 0xff;
    }
  }
  return out;
}
