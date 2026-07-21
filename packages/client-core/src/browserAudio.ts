/**
 * Browser streaming microphone (master plan §4.1) — the web SPA's `PcmAudioSource`.
 *
 * The Even G2 app gets PCM pushed by the glasses host and the mobile app owns a
 * native recorder; the browser has neither, so the web client captures the mic with
 * the Web Audio API and emits the same 16 kHz s16le mono PCM (base64, ~100 ms slices)
 * every `PcmAudioSource` produces, reusing the shared `pcm.ts` helpers. Uses
 * ScriptProcessor — adequate for a management-UI capture surface.
 */

import { downsampleTo16k, encodeBase64, floatToPcm16 } from "./pcm";
import type { PcmAudioSource } from "./pcmSource";

/**
 * Explain why the mic API is missing entirely. On an insecure origin (plain http on a
 * non-localhost host — e.g. hitting a dev server by LAN IP from a phone) mobile browsers
 * like Brave hide `navigator.mediaDevices` altogether, so capture never even reaches a
 * permission prompt. Distinguish that from a browser that genuinely lacks the API.
 */
function micUnavailableMessage(): string {
  const secure = typeof window === "undefined" ? true : window.isSecureContext;
  return secure
    ? "This browser does not support microphone capture."
    : "Microphone access needs a secure (HTTPS) connection. Open Tenir over https and try again.";
}

/** Turn a `getUserMedia` rejection into an actionable message rather than a bare denial. */
function micErrorMessage(err: unknown): string {
  const name = err instanceof Error ? err.name : "";
  switch (name) {
    case "NotAllowedError":
    case "PermissionDeniedError":
    case "SecurityError":
      return "Microphone permission denied. Allow mic access in your browser's site settings (and disable Brave Shields for this site) to record.";
    case "NotFoundError":
    case "DevicesNotFoundError":
      return "No microphone was found on this device.";
    case "NotReadableError":
    case "TrackStartError":
      return "The microphone is in use by another app. Close it and try again.";
    default:
      return "Could not access the microphone.";
  }
}

class BrowserAudioSource implements PcmAudioSource {
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private processor: ScriptProcessorNode | null = null;

  /** Why the last `requestPermission()` failed, surfaced to the user verbatim. */
  lastPermissionError?: string;

  async requestPermission(): Promise<boolean> {
    const media = typeof navigator === "undefined" ? undefined : navigator.mediaDevices;
    if (!media?.getUserMedia) {
      this.lastPermissionError = micUnavailableMessage();
      return false;
    }
    try {
      this.stream = await media.getUserMedia({ audio: true });
      this.lastPermissionError = undefined;
      return true;
    } catch (err) {
      this.lastPermissionError = micErrorMessage(err);
      return false;
    }
  }

  async start(onChunk: (base64Pcm: string) => void): Promise<boolean> {
    if (!this.stream) {
      const granted = await this.requestPermission();
      if (!granted) return false;
    }
    if (this.processor) return true; // already running

    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(this.stream!);
    const processor = ctx.createScriptProcessor(4096, 1, 1);
    processor.onaudioprocess = (e: AudioProcessingEvent) => {
      // Copy out of the reused input buffer before resampling.
      const input = new Float32Array(e.inputBuffer.getChannelData(0));
      const pcm = floatToPcm16(downsampleTo16k(input, ctx.sampleRate));
      if (pcm.length) onChunk(encodeBase64(pcm));
    };
    source.connect(processor);
    processor.connect(ctx.destination);

    this.ctx = ctx;
    this.source = source;
    this.processor = processor;
    return true;
  }

  async stop(): Promise<void> {
    this.processor?.disconnect();
    this.source?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    if (this.ctx) await this.ctx.close();
    this.processor = null;
    this.source = null;
    this.ctx = null;
    this.stream = null;
  }
}

/** A browser-microphone `PcmAudioSource` for the web capture client. */
export function browserAudioSource(): PcmAudioSource {
  return new BrowserAudioSource();
}
