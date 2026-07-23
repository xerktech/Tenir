/**
 * Phone-side History page (XERK-93).
 *
 * The dedicated review surface of the phone app: stored sessions from the api,
 * mirroring the web UI's History panel — search, a newest-first session list,
 * and a per-conversation detail (transcript with segment timing, retained-audio
 * playback, arm-then-confirm delete). Replaces browsing history through the
 * embedded web UI.
 *
 * Plain DOM, injected elements AND an injected api (defaulting to
 * `@tenir/client-core`'s `history`) so it unit-tests under jsdom without the
 * Even SDK or a fetch stub.
 */

import {
  ApiError,
  history as historyApi,
  type Conversation,
  type ConversationSummary,
  type SegmentView,
} from "@tenir/client-core";

/** The slice of the REST client the page drives — structural, so tests pass a fake. */
export interface HistoryApi {
  list(q?: string): Promise<ConversationSummary[]>;
  get(id: string): Promise<Conversation>;
  remove(id: string): Promise<void>;
  audioUrl(id: string): string;
}

export interface PhoneHistoryElements {
  list: HTMLElement; // the list section (hidden while the detail is open)
  form: HTMLFormElement; // the search form
  query: HTMLInputElement;
  status: HTMLElement; // spinner / error / empty-state slot above the rows
  rows: HTMLElement; // the session <ul>
  detail: HTMLElement; // the conversation detail card
  back: HTMLButtonElement;
  del: HTMLButtonElement; // arm-then-confirm delete
  meta: HTMLElement; // "date · duration · turns" line
  transcript: HTMLElement; // the segment block
  audio: HTMLElement; // audio player wrapper (hidden when no audio retained)
  audioEl: HTMLAudioElement;
  audioLink: HTMLAnchorElement;
}

/**
 * The page's elements, or null when the page doesn't carry them (tests that
 * mount only another slice) — the caller then skips the page rather than
 * failing the whole app.
 */
export function queryPhoneHistoryElements(doc: Document = document): PhoneHistoryElements | null {
  const byId = (id: string) => doc.getElementById(id);
  const list = byId("history-list");
  const form = byId("history-search");
  const query = byId("history-query");
  const status = byId("history-status");
  const rows = byId("history-rows");
  const detail = byId("history-detail");
  const back = byId("history-back");
  const del = byId("history-delete");
  const meta = byId("history-meta");
  const transcript = byId("history-transcript");
  const audio = byId("history-audio");
  const audioEl = byId("history-audio-el");
  const audioLink = byId("history-audio-link");
  if (
    !list || !form || !query || !status || !rows || !detail ||
    !back || !del || !meta || !transcript || !audio || !audioEl || !audioLink
  ) {
    return null;
  }
  return {
    list,
    form: form as HTMLFormElement,
    query: query as HTMLInputElement,
    status,
    rows,
    detail,
    back: back as HTMLButtonElement,
    del: del as HTMLButtonElement,
    meta,
    transcript,
    audio,
    audioEl: audioEl as HTMLAudioElement,
    audioLink: audioLink as HTMLAnchorElement,
  };
}

/** Render a millisecond span as m:ss (hours folded into minutes) — as the web UI does. */
export function formatDuration(ms: number): string {
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/** Segment timing rendered as "m:ss–m:ss" offsets from the session start. */
export function segmentTiming(s: SegmentView): string {
  return `${formatDuration(s.startMs)}–${formatDuration(s.endMs)}`;
}

/** User-facing error text (the web UI's errText). */
export function errText(err: unknown): string {
  return err instanceof ApiError ? `${err.status}: ${err.message}` : String(err);
}

export interface PhoneHistoryDeps {
  api?: HistoryApi;
  /** Errors that don't own a surface (detail open / delete failures) — the shell toasts them. */
  onError?: (message: string) => void;
  /** How long the armed delete lasts before quietly expiring. */
  disarmAfterMs?: number;
}

export class PhoneHistory {
  private readonly api: HistoryApi;
  private readonly onError?: (message: string) => void;
  private readonly disarmAfterMs: number;

  private current: Conversation | null = null;
  private armed = false;
  private disarmTimer: ReturnType<typeof setTimeout> | null = null;
  private listReq = 0; // stale-response guard for overlapping refreshes

  constructor(
    private readonly els: PhoneHistoryElements,
    deps: PhoneHistoryDeps = {},
  ) {
    this.api = deps.api ?? historyApi;
    this.onError = deps.onError;
    this.disarmAfterMs = deps.disarmAfterMs ?? 4000;

    this.els.form.addEventListener("submit", (e) => {
      e.preventDefault();
      void this.refresh();
    });
    this.els.back.addEventListener("click", () => this.showList());
    this.els.del.addEventListener("click", () => this.deleteClick());
  }

  /** Tab activation: refresh the list so a just-ended session shows up. */
  open(): void {
    void this.refresh();
  }

  /** Sign-out: drop everything loaded — the next user must not see this household's data. */
  reset(): void {
    this.listReq += 1; // orphan any in-flight listing
    this.els.query.value = "";
    this.els.rows.replaceChildren();
    this.els.status.replaceChildren();
    this.showList();
  }

  /** (Re)load the session list for the current search query. */
  async refresh(): Promise<void> {
    const req = ++this.listReq;
    this.renderSpinner();
    try {
      const rows = await this.api.list(this.els.query.value.trim() || undefined);
      if (req !== this.listReq) return; // a newer refresh superseded this one
      this.renderRows(rows);
    } catch (err) {
      if (req !== this.listReq) return;
      this.renderLoadError(errText(err));
    }
  }

  // ---- list rendering --------------------------------------------------------

  private renderSpinner(): void {
    const row = this.make("span", "spinner-row");
    row.setAttribute("role", "status");
    const dot = this.make("span", "spinner");
    dot.setAttribute("aria-hidden", "true");
    row.append(dot, "Loading…");
    this.els.status.replaceChildren(row);
  }

  /** The web UI's dashed empty-state block. */
  private emptyState(title: string, hint: string): HTMLElement {
    const box = this.make("div", "empty");
    box.appendChild(this.make("p", "empty-title", title));
    box.appendChild(this.make("p", "empty-hint", hint));
    return box;
  }

  private renderRows(rows: ConversationSummary[]): void {
    this.els.status.replaceChildren();
    this.els.rows.replaceChildren();
    if (rows.length === 0) {
      this.els.status.appendChild(
        this.emptyState("No conversations yet", "Captured conversations will appear here."),
      );
      return;
    }
    for (const c of rows) {
      const li = this.make("li", "history-item");
      const button = this.make("button", "history-open") as HTMLButtonElement;
      button.type = "button";
      button.appendChild(this.make("span", "history-when", new Date(c.startedAt).toLocaleString()));
      button.appendChild(
        this.make(
          "span",
          "history-meta",
          `${formatDuration(c.durationMs)} · ${c.segmentCount} turns · ${c.status}`,
        ),
      );
      button.addEventListener("click", () => void this.openDetail(c.id));
      li.appendChild(button);
      this.els.rows.appendChild(li);
    }
  }

  /* A failed listing must not render as an empty section, indistinguishable
     from having recorded nothing (XERK-58 on the web) — say so, offer a retry. */
  private renderLoadError(message: string): void {
    this.els.rows.replaceChildren();
    const box = this.emptyState("Could not load history", message);
    const retry = this.make("button", "btn btn-secondary", "Retry") as HTMLButtonElement;
    retry.type = "button";
    retry.addEventListener("click", () => void this.refresh());
    this.els.status.replaceChildren(box, retry);
  }

  // ---- conversation detail ---------------------------------------------------

  private async openDetail(id: string): Promise<void> {
    try {
      this.showDetail(await this.api.get(id));
    } catch (err) {
      this.onError?.(errText(err));
    }
  }

  private showDetail(conv: Conversation): void {
    this.current = conv;
    this.disarm();
    this.els.meta.textContent = `${new Date(conv.startedAt).toLocaleString()} · ${formatDuration(
      conv.durationMs,
    )} · ${conv.segmentCount} turns`;

    // A session can hold no turns at all (nothing was said, or the transcript
    // was lost). Say so — an empty block reads as a detail that failed to open.
    if (conv.segments.length === 0) {
      this.els.transcript.replaceChildren(
        this.make("p", "muted", "No transcript was recorded for this session."),
      );
    } else {
      const frag = this.els.transcript.ownerDocument.createDocumentFragment();
      for (const s of conv.segments) {
        const item = this.make("div", "item");
        item.appendChild(this.make("span", "muted", segmentTiming(s)));
        item.append(` ${s.text}`);
        frag.appendChild(item);
      }
      this.els.transcript.replaceChildren(frag);
    }

    if (conv.hasAudio) {
      const url = this.api.audioUrl(conv.id);
      this.els.audioEl.src = url;
      this.els.audioLink.href = url;
      this.els.audio.hidden = false;
    } else {
      this.stopAudio();
      this.els.audio.hidden = true;
    }

    this.els.list.hidden = true;
    this.els.detail.hidden = false;
  }

  private showList(): void {
    this.current = null;
    this.disarm();
    this.stopAudio();
    this.els.detail.hidden = true;
    this.els.list.hidden = false;
  }

  // ---- delete (Turma's arm-then-confirm pattern, as on the web) ---------------

  private deleteClick(): void {
    if (!this.current) return;
    if (!this.armed) {
      this.armed = true;
      this.els.del.textContent = "Confirm delete";
      this.els.del.classList.add("armed");
      // An accidental click quietly expires instead of leaving a live trigger.
      this.disarmTimer = setTimeout(() => this.disarm(), this.disarmAfterMs);
      return;
    }
    const id = this.current.id;
    this.disarm();
    this.api
      .remove(id)
      .then(() => {
        this.showList();
        void this.refresh();
      })
      .catch((err) => this.onError?.(errText(err)));
  }

  private disarm(): void {
    if (this.disarmTimer) {
      clearTimeout(this.disarmTimer);
      this.disarmTimer = null;
    }
    this.armed = false;
    this.els.del.textContent = "Delete";
    this.els.del.classList.remove("armed");
  }

  // ---- helpers ---------------------------------------------------------------

  private stopAudio(): void {
    try {
      this.els.audioEl.pause();
    } catch {
      /* jsdom has no media implementation */
    }
    this.els.audioEl.removeAttribute("src");
  }

  private make(tag: string, className: string, text?: string): HTMLElement {
    const el = this.els.status.ownerDocument.createElement(tag);
    el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
}
