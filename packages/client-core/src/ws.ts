/**
 * Api WebSocket client.
 *
 * One bidirectional WSS to our api: raw PCM goes up as binary frames; captions
 * come down as JSON. Long-lived `sessionId` lets a session continue across brief
 * drops (reconnect-with-resume). Lives in the shared core so any capture client
 * (glasses, web, Android) speaks the same wire protocol.
 */

import type {
  CaptionFinal,
  CaptionPartial,
  ClientMessage,
  Cue,
  CueLevel,
  ErrorMessage,
  Lang,
  MicSource,
  Pong,
  ServerMessage,
  SessionReady,
} from "@tenir/contract";

import { withToken } from "./auth";

export interface SessionParams {
  micSource: MicSource;
  sourceLang?: Lang;
  // How eagerly the api should surface private context cues (XERK-81). Chosen by
  // the user in the client UI and sent on session.start; omitted → server default.
  cueLevel?: CueLevel;
}

export interface ApiHandlers {
  onReady?: (m: SessionReady) => void;
  onPartial?: (m: CaptionPartial) => void;
  onFinal?: (m: CaptionFinal) => void;
  onCue?: (m: Cue) => void;
  onPong?: (m: Pong) => void;
  onError?: (m: ErrorMessage) => void;
  onConnectionChange?: (state: "connecting" | "open" | "closed") => void;
}

// Pause uploading if the socket's send buffer backs up (master plan §4.1: backpressure-aware).
const MAX_BUFFERED_BYTES = 256 * 1024;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 16000;

export class ApiClient {
  private ws: WebSocket | null = null;
  private sessionId: string | null = null;
  private params: SessionParams | null = null;
  private handlers: ApiHandlers;
  private url: string;
  private reconnectAttempt = 0;
  private closedByUser = false;
  // Set on a fatal/policy close (bad or expired token, capture off): reconnecting
  // can't fix it and would hammer the api, so we stop and surface it instead.
  private fatal = false;

  constructor(url: string, handlers: ApiHandlers = {}) {
    this.url = url;
    this.handlers = handlers;
  }

  /** The authoritative session id (from session.ready), or null before ready. */
  get currentSessionId(): string | null {
    return this.sessionId;
  }

  /**
   * Open the socket and start a session. Safe to call once; reconnect is internal.
   * Pass `resumeSessionId` (e.g. one restored after backgrounding) to resume a
   * prior session so diarization continuity survives the gap (master plan §4.2).
   */
  start(params: SessionParams, resumeSessionId?: string): void {
    this.params = params;
    this.closedByUser = false;
    this.fatal = false;
    if (resumeSessionId) this.sessionId = resumeSessionId;
    this.connect();
  }

  private connect(): void {
    this.handlers.onConnectionChange?.("connecting");
    // Carry the bearer token as ?token= (the browser WS API can't set an
    // Authorization header); the token is absent until login (master plan §7).
    const ws = new WebSocket(withToken(this.url));
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      this.reconnectAttempt = 0;
      this.handlers.onConnectionChange?.("open");
      // Resume the prior session if we have an id, else start fresh.
      this.send({
        type: "session.start",
        micSource: this.params!.micSource,
        sourceLang: this.params!.sourceLang,
        cueLevel: this.params!.cueLevel,
        ...(this.sessionId ? { sessionId: this.sessionId } : {}),
      });
    };

    ws.onmessage = (ev) => this.dispatch(ev.data);

    ws.onclose = (ev) => {
      this.handlers.onConnectionChange?.("closed");
      // 1008 (policy violation) is the api rejecting the connection itself —
      // missing/invalid/expired token, or capture disabled. It is not a transient
      // drop: reconnecting loops forever without re-auth, so stop and surface it so
      // the app can prompt a re-login instead of silently hammering the api.
      if (ev.code === 1008) {
        this.fatal = true;
        this.handlers.onError?.({
          type: "error",
          code: "unauthorized",
          message: "connection rejected — please sign in again",
          fatal: true,
        });
      }
      if (!this.closedByUser && !this.fatal) this.scheduleReconnect();
    };

    ws.onerror = () => {
      // onclose will follow; reconnect handled there.
    };
  }

  private scheduleReconnect(): void {
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** this.reconnectAttempt, RECONNECT_MAX_MS);
    this.reconnectAttempt += 1;
    setTimeout(() => {
      if (!this.closedByUser && !this.fatal) this.connect();
    }, delay);
  }

  private dispatch(data: unknown): void {
    if (typeof data !== "string") return; // server only sends JSON text frames
    let msg: ServerMessage;
    try {
      msg = JSON.parse(data) as ServerMessage;
    } catch {
      return;
    }
    switch (msg.type) {
      case "session.ready":
        this.sessionId = msg.sessionId;
        this.handlers.onReady?.(msg);
        break;
      case "caption.partial":
        this.handlers.onPartial?.(msg);
        break;
      case "caption.final":
        this.handlers.onFinal?.(msg);
        break;
      case "cue":
        this.handlers.onCue?.(msg);
        break;
      case "pong":
        this.handlers.onPong?.(msg);
        break;
      case "error":
        // A fatal error won't be cured by reconnecting (e.g. capture disabled):
        // stop the reconnect loop and close, then surface it to the app.
        if (msg.fatal) {
          this.fatal = true;
          this.ws?.close();
        }
        this.handlers.onError?.(msg);
        break;
    }
  }

  /** Stream a chunk of PCM. Dropped (not buffered indefinitely) if backpressured. */
  sendAudio(pcm: Uint8Array): boolean {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    if (ws.bufferedAmount > MAX_BUFFERED_BYTES) return false; // backpressure: drop, don't pile up
    ws.send(pcm);
    return true;
  }

  send(msg: ClientMessage): void {
    const ws = this.ws;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  switchMic(micSource: MicSource): void {
    if (this.params) this.params.micSource = micSource;
    this.send({ type: "mic.switch", micSource });
  }

  ping(): void {
    this.send({ type: "ping", t: Date.now() });
  }

  /** End the session and close the socket; no reconnect afterwards. */
  stop(): void {
    this.closedByUser = true;
    this.send({ type: "session.end" });
    this.ws?.close();
    this.ws = null;
  }
}
