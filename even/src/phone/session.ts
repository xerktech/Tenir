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

import { isPinnedToBottom } from "@tenir/client-core";

import type { CueCard } from "../lens/layout";

/**
 * A cue that has already had its turn in the band and now sits inline in the
 * transcript for review (XERK-108), anchored after the finalized turn it
 * followed. `afterIndex` is that turn's index in `segments`; a value outside the
 * range (its turn scrolled off, or the cue landed before any speech) leads the
 * transcript. `id` is a stable key so its expand state survives caption redraws.
 */
export interface PastCue extends CueCard {
  id: string;
  afterIndex: number;
}

export interface LiveSessionView {
  recording: boolean;
  connection: "connecting" | "open" | "closed";
  segments: string[]; // finalized turns
  partial: string; // current live hypothesis
  cue: CueCard | null; // the current private context cue (XERK-81), or none
  pastCues: PastCue[]; // released cues embedded in the transcript for review (XERK-108)
}

/** One row of the rendered transcript: a finalized turn or a reviewed cue. */
type TranscriptRow = { kind: "segment"; text: string } | { kind: "cue"; cue: PastCue };

/**
 * Interleave finalized turns with reviewed cues (XERK-108): each cue lands right
 * after the turn it was anchored to, and a cue whose anchor is out of range —
 * before any speech, or a turn since scrolled off — leads the transcript. The
 * index-anchored counterpart to client-core's id-anchored `liveTranscript`, kept
 * here because the phone mirror carries plain-string turns without ids.
 */
export function liveTranscriptRows(segments: string[], pastCues: PastCue[]): TranscriptRow[] {
  const byIndex = new Map<number, PastCue[]>();
  const leading: PastCue[] = [];
  for (const cue of pastCues) {
    if (cue.afterIndex >= 0 && cue.afterIndex < segments.length) {
      const list = byIndex.get(cue.afterIndex);
      if (list) list.push(cue);
      else byIndex.set(cue.afterIndex, [cue]);
    } else {
      leading.push(cue);
    }
  }
  const rows: TranscriptRow[] = leading.map((cue) => ({ kind: "cue", cue }));
  segments.forEach((text, i) => {
    rows.push({ kind: "segment", text });
    for (const cue of byIndex.get(i) ?? []) rows.push({ kind: "cue", cue });
  });
  return rows;
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
  // Which reviewed cues are expanded, by cue id (XERK-108). Held on the page so
  // an expanded past cue stays open across the frequent caption redraws — the
  // transcript is rebuilt wholesale on every update, unlike React's live tree.
  private expanded = new Set<string>();

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

    const hasText =
      view.recording &&
      (view.segments.length > 0 || view.pastCues.length > 0 || view.partial !== "");
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
      // Nothing on screen to keep open; forget any stale expand state.
      this.expanded.clear();
    } else {
      const box = this.els.text;
      // Was the viewer following the live feed (at the bottom) before this
      // update? Measure before swapping the rows in.
      const pinned = isPinnedToBottom(box);
      const doc = box.ownerDocument;
      const frag = doc.createDocumentFragment();
      // Interleave finalized turns with reviewed cues (XERK-108): a released cue
      // sits inline as a collapsed dropdown after the turn that triggered it, so
      // it can be re-read without disturbing the live cue card pinned above.
      for (const row of liveTranscriptRows(view.segments, view.pastCues)) {
        if (row.kind === "segment") {
          const li = doc.createElement("li");
          li.textContent = row.text;
          frag.appendChild(li);
        } else {
          frag.appendChild(this.buildCueRow(row.cue));
        }
      }
      if (view.partial) {
        const li = doc.createElement("li");
        li.className = "partial";
        li.textContent = view.partial;
        frag.appendChild(li);
      }
      box.replaceChildren(frag);
      // Follow the newest caption inside the transcript's OWN scroll box, so the
      // cue card pinned above it stays in view (XERK-103) — scrolling the page
      // would carry the cue out of view. Only stick while already at the bottom;
      // a viewer who scrolled up to re-read is left where they are.
      if (pinned) box.scrollTop = box.scrollHeight;
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

  /**
   * A reviewed cue embedded in the transcript (XERK-108): an inline collapsed
   * dropdown — "▸ ✦ <title>" — that expands in place to reveal the body, the
   * phone counterpart to the web/mobile CueDisclosure. Its open state is keyed
   * off `this.expanded`, so a rebuilt row comes back in whatever state the
   * viewer left it, and the toggle flips the DOM directly (no full redraw).
   */
  private buildCueRow(cue: PastCue): HTMLElement {
    const doc = this.els.text.ownerDocument;
    const open = this.expanded.has(cue.id);
    const bodyId = `session-cue-body-${cue.id}`;

    const li = doc.createElement("li");
    li.className = "session-cue-line";

    const button = doc.createElement("button");
    button.type = "button";
    button.className = "cue-inline";
    button.setAttribute("aria-expanded", String(open));
    button.setAttribute("aria-controls", bodyId);
    button.title = open ? "Hide cue detail" : "Show cue detail";
    const caret = this.make("span", "cue-inline-caret", open ? "▾" : "▸");
    caret.setAttribute("aria-hidden", "true");
    const mark = this.make("span", "cue-inline-mark", "✦");
    mark.setAttribute("aria-hidden", "true");
    const titleEl = this.make("span", "cue-inline-title", cue.title);
    button.append(caret, mark, titleEl);

    const body = this.make("p", "cue-inline-body", cue.body);
    body.id = bodyId;
    body.hidden = !open;

    button.addEventListener("click", () => {
      const nowOpen = !this.expanded.has(cue.id);
      if (nowOpen) this.expanded.add(cue.id);
      else this.expanded.delete(cue.id);
      button.setAttribute("aria-expanded", String(nowOpen));
      button.title = nowOpen ? "Hide cue detail" : "Show cue detail";
      caret.textContent = nowOpen ? "▾" : "▸";
      body.hidden = !nowOpen;
    });

    li.append(button, body);
    return li;
  }

  private make(tag: string, className: string, text: string): HTMLElement {
    const el = this.els.text.ownerDocument.createElement(tag);
    el.className = className;
    el.textContent = text;
    return el;
  }
}
