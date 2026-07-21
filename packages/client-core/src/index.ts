/**
 * @tenir/client-core — the shared TS core.
 *
 * API + WS client, auth/token handling, api configuration, and the capture
 * session state machine, written once and reused by every TS frontend: the Even
 * G2 glasses app (`even/`), the web SPA (`web/`), and the Android app
 * (`mobile/`). The only client-specific piece that does *not* live here is
 * native audio capture and the lens render loop, which differ per platform.
 */

export * from "./config";
export * from "./auth";
export * from "./api";
export * from "./ws";
export * from "./pcm";
export * from "./pcmSource";
export * from "./captureSession";
export * from "./disclosures";
export * from "./browserAudio";
