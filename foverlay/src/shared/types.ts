/**
 * Shared shapes crossing the background ↔ UI channel bus.
 *
 * Mirrors the state the upstream Even Hub app shared between its lens
 * controller and phone pages (even/src/lens/controller.ts `Mutable`,
 * even/src/phone/session.ts `LiveSessionView`), trimmed to what the miniapp
 * phone page renders.
 */

export type ConnectionState = "connecting" | "open" | "closed";

/** One finalized turn, keyed so a translation can pair to it (XERK-160). */
export interface TenirSegment {
  id: string;
  text: string;
  /** Detected spoken language of the turn — tags a translated turn's original text. */
  lang?: string;
  /** English translation of a non-English turn, once it lands. */
  translation?: string;
}

/**
 * A private context cue (XERK-81) embedded in the phone transcript for review
 * (XERK-108): anchored after the finalized turn it followed. `afterIndex`
 * outside the segments range means it leads the transcript.
 */
export interface TenirCue {
  id: string;
  title: string;
  body: string;
  /** Live-source attribution (XERK-120), e.g. "BBC News". */
  source?: string;
  afterIndex: number;
}

/** The song currently recognized playing (XERK-184), phone-mirror form. */
export interface TenirSong {
  id: string;
  title: string;
  artist: string;
}

/** Sign-in state as the phone page needs it. */
export interface TenirAuthState {
  signedIn: boolean;
  username: string;
  /** Friendly host-only form for prefilling the server field. */
  serverUrl: string;
}

/** The live session mirror. */
export interface TenirLiveState {
  recording: boolean;
  connection: ConnectionState;
  segments: TenirSegment[];
  partial: string;
  cues: TenirCue[];
  song: TenirSong | null;
}

/** Full hydration snapshot, sent on every WebView open. */
export interface TenirSnapshot {
  auth: TenirAuthState;
  live: TenirLiveState;
}

// ---- RPC payloads -----------------------------------------------------------

export interface LoginRequest {
  serverUrl: string;
  username: string;
  password: string;
}

export type LoginResult = { ok: true; username: string } | { ok: false; error: string };

/** A generic proxied REST call (the UI can't fetch cross-origin from file://). */
export interface ProxyFetchRequest {
  path: string;
  method?: "GET" | "POST" | "DELETE" | "PUT" | "PATCH";
  body?: unknown;
}

export type ProxyFetchResult =
  | { ok: true; data: unknown }
  | { ok: false; error: string; status?: number };

export type StartResult = { ok: boolean; error?: string };
