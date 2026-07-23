/**
 * Phone-side Session page (XERK-93).
 *
 * The dedicated live-session surface of the phone app. The glasses session's
 * WS client runs in this same WebView (the phone side of the Even app), so the
 * captions the lens renders are already here — this page mirrors them
 * full-page in real time, replacing the XERK-85 strip that sat squished above
 * the embedded web UI. Idle it explains how a session starts (from the
 * glasses); while one records it shows the connection state and the running
 * transcript, following the newest text.
 *
 * Plain DOM, injected elements (like phone/login.ts) so it unit-tests under
 * jsdom without the Even SDK.
 */

import type { CueCard } from "../lens/layout";

export interface LiveSessionView {
  recording: boolean;
  connection: "connecting" | "open" | "closed";
  segments: string[]; // finalized turns
  partial: string; // current live hypothesis
  cue: CueCard | null; // the current private context cue (XERK-81), or none
}

export interface SessionPageElements {
  badge: HTMLElement; // connection/idle state pill in the page header
  dot: HTMLElement; // pulsing "live" dot, shown only while recording
  cue: HTMLElement; // the private context cue card (XERK-81), above the transcript
  empty: HTMLElement; // the empty-state block (idle / waiting for speech)
  emptyTitle: HTMLElement;
  emptyHint: HTMLElement;
  text: HTMLElement; // the transcript <ul>
}

/**
 * The page's elements, or null when the page doesn't carry them (tests that
 * mount only the login slice, older markup) — the caller then skips the mirror
 * rather than failing the whole app.
 */
export function querySessionPageElements(doc: Document = document): SessionPageElements | null {
  const badge = doc.getElementById("session-badge");
  const dot = doc.getElementById("session-dot");
  const cue = doc.getElementById("session-cue");
  const empty = doc.getElementById("session-empty");
  const emptyTitle = doc.getElementById("session-empty-title");
  const emptyHint = doc.getElementById("session-empty-hint");
  const text = doc.getElementById("session-text");
  if (!badge || !dot || !cue || !empty || !emptyTitle || !emptyHint || !text) return null;
  return { badge, dot, cue, empty, emptyTitle, emptyHint, text };
}

/** The in-session one-word state, honest about connectivity like the lens (XERK-82). */
export function sessionStatus(view: Pick<LiveSessionView, "connection">): string {
  if (view.connection === "connecting") return "connecting…";
  if (view.connection === "closed") return "reconnecting…";
  return "listening";
}

export interface SessionPageCallbacks {
  /**
   * A session just started (recording flipped false → true): the shell brings
   * the Session page to the front so the live transcript is what the wearer
   * sees, wherever they were browsing.
   */
  onRecordingStart?: () => void;
}

export class SessionPage {
  private wasRecording = false;

  constructor(
    private readonly els: SessionPageElements,
    private readonly callbacks: SessionPageCallbacks = {},
  ) {}

  /** Re-render from the session state. Cheap: tens of short rows, rebuilt in one fragment. */
  update(view: LiveSessionView): void {
    const started = view.recording && !this.wasRecording;
    this.wasRecording = view.recording;

    this.els.dot.hidden = !view.recording;
    this.els.badge.textContent = view.recording ? sessionStatus(view) : "idle";
    // The pill is accented only while captions are actually flowing.
    this.els.badge.className =
      view.recording && view.connection === "open" ? "badge-accent" : "badge-neutral";

    // The private context cue (XERK-81): a bordered accent card above the
    // transcript, shown only while a cue is live and a session is recording.
    this.renderCue(view.recording ? view.cue : null);

    const hasText = view.recording && (view.segments.length > 0 || view.partial !== "");
    this.els.text.hidden = !hasText;
    this.els.empty.hidden = hasText;
    if (!hasText) {
      // Idle explains how a session starts (on the glasses — this page has no
      // Record button on purpose); in-session it says captions are coming.
      this.els.emptyTitle.textContent = view.recording ? "Listening for speech…" : "No session running";
      this.els.emptyHint.textContent = view.recording
        ? "Captions appear here as they are heard."
        : "Tap your glasses to start a session.";
      this.els.text.replaceChildren();
    } else {
      const doc = this.els.text.ownerDocument;
      const frag = doc.createDocumentFragment();
      for (const segment of view.segments) {
        const li = doc.createElement("li");
        li.textContent = segment;
        frag.appendChild(li);
      }
      if (view.partial) {
        const li = doc.createElement("li");
        li.className = "partial";
        li.textContent = view.partial;
        frag.appendChild(li);
      }
      this.els.text.replaceChildren(frag);
      // Follow the newest text, like the lens: the page is a live feed.
      // (Guarded: jsdom doesn't implement scrollIntoView.)
      const last = this.els.text.lastElementChild as HTMLElement | null;
      if (last && typeof last.scrollIntoView === "function") last.scrollIntoView({ block: "end" });
    }

    // After the render, so the page is current the moment it is brought forward.
    if (started) this.callbacks.onRecordingStart?.();
  }

  /** Render (or hide) the live cue card: the title (uppercased, accent) over its body. */
  private renderCue(cue: CueCard | null): void {
    if (!cue) {
      this.els.cue.hidden = true;
      this.els.cue.replaceChildren();
      return;
    }
    const doc = this.els.cue.ownerDocument;
    const title = doc.createElement("div");
    title.className = "session-cue-title";
    title.textContent = cue.title;
    const body = doc.createElement("div");
    body.className = "session-cue-body";
    body.textContent = cue.body;
    this.els.cue.replaceChildren(title, body);
    this.els.cue.hidden = false;
  }
}
