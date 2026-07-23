/**
 * Phone-side live transcript panel (XERK-85).
 *
 * The glasses session's WS client runs in this same WebView (the phone side of
 * the Even app), so the captions the lens renders are already here — this panel
 * mirrors them into the signed-in phone view in real time, above the embedded
 * web UI. It appears only while a session is recording and follows the newest
 * text (auto-scrolled to the bottom).
 *
 * Plain DOM, injected elements (like phone/login.ts) so it unit-tests under
 * jsdom without the Even SDK.
 */

export interface LiveTranscriptView {
  recording: boolean;
  connection: "connecting" | "open" | "closed";
  segments: string[]; // finalized turns
  partial: string; // current live hypothesis
}

export interface PhoneTranscriptElements {
  panel: HTMLElement; // the whole strip (hidden outside a session)
  status: HTMLElement; // small connection/listening state text
  text: HTMLElement; // the scrolling <ul class="transcript">
}

/**
 * The panel's elements, or null when the page doesn't carry the panel (tests
 * that mount only the login slice, older markup) — the caller then skips the
 * mirror rather than failing the whole app.
 */
export function queryPhoneTranscriptElements(doc: Document = document): PhoneTranscriptElements | null {
  const panel = doc.getElementById("live-transcript");
  const status = doc.getElementById("live-status");
  const text = doc.getElementById("live-text");
  if (!panel || !status || !text) return null;
  return { panel, status, text };
}

/** The strip's one-word state, honest about connectivity like the lens (XERK-82). */
export function transcriptStatus(view: Pick<LiveTranscriptView, "connection">): string {
  if (view.connection === "connecting") return "connecting…";
  if (view.connection === "closed") return "reconnecting…";
  return "listening";
}

export class PhoneTranscript {
  constructor(private readonly els: PhoneTranscriptElements) {}

  /** Re-render from the session state. Cheap: tens of short rows, rebuilt in one fragment. */
  update(view: LiveTranscriptView): void {
    if (!view.recording) {
      this.els.panel.hidden = true;
      return;
    }
    this.els.panel.hidden = false;
    this.els.status.textContent = transcriptStatus(view);

    const frag = this.els.text.ownerDocument.createDocumentFragment();
    for (const segment of view.segments) {
      const li = this.els.text.ownerDocument.createElement("li");
      li.textContent = segment;
      frag.appendChild(li);
    }
    if (view.partial) {
      const li = this.els.text.ownerDocument.createElement("li");
      li.className = "partial";
      li.textContent = view.partial;
      frag.appendChild(li);
    }
    if (!frag.hasChildNodes()) {
      const li = this.els.text.ownerDocument.createElement("li");
      li.className = "partial";
      li.textContent = "Listening for speech…";
      frag.appendChild(li);
    }
    this.els.text.replaceChildren(frag);
    // Follow the newest text, like the lens: the panel is a live feed.
    this.els.text.scrollTop = this.els.text.scrollHeight;
  }
}
