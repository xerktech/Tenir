/**
 * Microphone capture (master plan §4.1).
 *
 * The host delivers 16 kHz s16le mono PCM in ~100ms `audioEvent` chunks
 * (3200 bytes / 1600 samples). We don't resample — it matches the api STT
 * input natively. Mic *source* (g2 vs phone) is chosen at the permission/host
 * level; at runtime we track the active source and tell the api via
 * `mic.switch` when it changes.
 */

import type { AudioEventPayload, EvenAppBridge } from "@evenrealities/even_hub_sdk";

export class AudioCapture {
  private bridge: EvenAppBridge;
  private active = false;

  constructor(bridge: EvenAppBridge) {
    this.bridge = bridge;
  }

  get isActive(): boolean {
    return this.active;
  }

  /** Start the mic. Requires a successful `createStartUpPageContainer` first. */
  async start(): Promise<boolean> {
    if (this.active) return true;
    this.active = await this.bridge.audioControl(true);
    return this.active;
  }

  async stop(): Promise<void> {
    if (!this.active) return;
    await this.bridge.audioControl(false);
    this.active = false;
  }
}

/**
 * Normalize an audioEvent payload to a Uint8Array of PCM bytes.
 * The host may deliver `audioPcm` as a Uint8Array, a number[], or base64.
 */
export function pcmBytes(payload: AudioEventPayload): Uint8Array {
  const raw = payload.audioPcm as unknown;
  if (raw instanceof Uint8Array) return raw;
  if (Array.isArray(raw)) return Uint8Array.from(raw as number[]);
  if (typeof raw === "string") {
    const bin = atob(raw);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  return new Uint8Array(0);
}
