/**
 * Phone-side Session page (XERK-93).
 *
 * The dedicated live-session surface of the phone app. The glasses session's
 * WS client runs in this same WebView (the phone side of the Even app), so the
 * captions the lens renders are already here — this page mirrors them
 * full-page in real time, replacing the XERK-85 strip that sat squished above
 * the embedded web UI. Idle it offers Start; while one records it shows the
 * connection state, a Stop button and the running transcript, following the
 * newest text.
 *
 * The Start/Stop pair (XERK-116) drives the same session state machine the
 * glasses tap does — the lens controller hands this page its start/stop through
 * `setControls`, so a session no longer has to be begun and ended on the
 * glasses. Without a lens (a plain browser in dev) no controls are attached and
 * the row stays hidden, since there would be nothing to record with.
 *
 * Plain DOM, injected elements (like phone/login.ts) so it unit-tests under
 * jsdom without the Even SDK.
 */

import {
  cueCountdownLabel,
  currentLyricIndex,
  isPinnedToBottom,
  lyricWindow,
  type LiveSong,
} from "@tenir/client-core";

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

/**
 * One finalized turn as the phone mirror renders it: the spoken text, plus the
 * English translation of a non-English turn once it lands (XERK-160). A plain
 * string is accepted as shorthand for a turn with no translation.
 */
export interface SessionSegment {
  text: string;
  /** Detected spoken language of the turn — tags a translated turn's original text. */
  lang?: string;
  translation?: string;
}

/** Normalize the string shorthand to the object shape. */
export function asSegment(seg: string | SessionSegment): SessionSegment {
  return typeof seg === "string" ? { text: seg } : seg;
}

export interface LiveSessionView {
  recording: boolean;
  connection: "connecting" | "open" | "closed";
  segments: Array<string | SessionSegment>; // finalized turns
  partial: string; // current live hypothesis
  cue: CueCard | null; // the current private context cue (XERK-81), or none
  cueSecondsLeft?: number; // seconds until that cue auto-dismisses (XERK-110)
  pastCues: PastCue[]; // released cues embedded in the transcript for review (XERK-108)
  song?: LiveSong | null; // the song recognized playing, whose lyrics scroll (XERK-184)
}

/** One row of the rendered transcript: a finalized turn or a reviewed cue. */
type TranscriptRow = { kind: "segment"; segment: SessionSegment } | { kind: "cue"; cue: PastCue };

/**
 * Interleave finalized turns with reviewed cues (XERK-108): each cue lands right
 * after the turn it was anchored to, and a cue whose anchor is out of range —
 * before any speech, or a turn since scrolled off — leads the transcript. The
 * index-anchored counterpart to client-core's id-anchored `liveTranscript`, kept
 * here because the phone mirror carries index-anchored turns.
 */
export function liveTranscriptRows(
  segments: Array<string | SessionSegment>,
  pastCues: PastCue[],
): TranscriptRow[] {
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
  segments.forEach((seg, i) => {
    rows.push({ kind: "segment", segment: asSegment(seg) });
    for (const cue of byIndex.get(i) ?? []) rows.push({ kind: "cue", cue });
  });
  return rows;
}

export interface SessionPageElements {
  badge: HTMLElement; // connection/idle state pill in the page header
  dot: HTMLElement; // pulsing "live" dot, shown only while recording
  controls: HTMLElement; // the Start/Stop row (XERK-116)
  start: HTMLButtonElement;
  stop: HTMLButtonElement;
  cue: HTMLElement; // the private context cue card (XERK-81), above the transcript
  song: HTMLElement; // the recognized-song lyric card (XERK-184), above the transcript
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
  const controls = doc.getElementById("session-controls");
  const start = doc.getElementById("session-start");
  const stop = doc.getElementById("session-stop");
  const cue = doc.getElementById("session-cue");
  const song = doc.getElementById("session-song");
  const empty = doc.getElementById("session-empty");
  const emptyTitle = doc.getElementById("session-empty-title");
  const emptyHint = doc.getElementById("session-empty-hint");
  const text = doc.getElementById("session-text");
  if (!badge || !dot || !controls || !start || !stop) return null;
  if (!cue || !song || !empty || !emptyTitle || !emptyHint || !text) return null;
  return {
    badge,
    dot,
    controls,
    start: start as HTMLButtonElement,
    stop: stop as HTMLButtonElement,
    cue,
    song,
    empty,
    emptyTitle,
    emptyHint,
    text,
  };
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

/**
 * Start/stop a session from the phone (XERK-116). The lens controller supplies
 * these — they are the very same transitions a tap on the glasses drives, so
 * either surface can begin or end a session and both stay in step.
 */
export interface SessionControls {
  start(): void;
  stop(): void;
}

export class SessionPage {
  // Whether a session is running, as of the last render: the edge into it fires
  // `onRecordingStart`, and it decides which of Start / Stop is showing.
  private recording = false;
  // Start/stop, once the lens controller has handed them over (XERK-116). Null
  // until then — and forever in a browser with no glasses bridge, where the
  // control row stays hidden because there is nothing to record with.
  private controls: SessionControls | null = null;
  // Which reviewed cues are expanded, by cue id (XERK-108). Held on the page so
  // an expanded past cue stays open across the frequent caption redraws — the
  // transcript is rebuilt wholesale on every update, unlike React's live tree.
  private expanded = new Set<string>();
  // The live cue card's countdown element (XERK-110) while one is on screen, so
  // the ticker can repaint just that number.
  private countdown: HTMLElement | null = null;
  // The song currently on screen (XERK-184) and the element its lyric lines live
  // in, so `tickSong` can advance the scroll without redrawing the transcript —
  // the same targeted-repaint pattern the cue countdown uses.
  private song: LiveSong | null = null;
  private songLines: HTMLElement | null = null;

  constructor(
    private readonly els: SessionPageElements,
    private readonly callbacks: SessionPageCallbacks = {},
  ) {
    // Wired once here rather than per render: the buttons are permanent page
    // furniture, only their visibility changes with the session (XERK-116).
    this.els.start.addEventListener("click", () => this.controls?.start());
    this.els.stop.addEventListener("click", () => this.controls?.stop());
  }

  /**
   * Hand the page the session transitions (XERK-116) and reveal the Start/Stop
   * row. Called by the lens controller once it owns a session state machine —
   * which may be well after this page was constructed (the bridge can resolve
   * late), so the row is revealed here rather than in the markup.
   */
  setControls(controls: SessionControls): void {
    this.controls = controls;
    this.renderControls();
  }

  /** Re-render from the session state. Cheap: tens of short rows, rebuilt in one fragment. */
  update(view: LiveSessionView): void {
    const started = view.recording && !this.recording;
    this.recording = view.recording;
    this.renderControls();

    this.els.dot.hidden = !view.recording;
    this.els.badge.textContent = view.recording ? sessionStatus(view) : "idle";
    // The pill is accented only while captions are actually flowing.
    this.els.badge.className =
      view.recording && view.connection === "open" ? "badge-accent" : "badge-neutral";

    // The private context cue (XERK-81): a bordered accent card above the
    // transcript, shown only while a cue is live and a session is recording,
    // with the countdown to its dismissal top-right (XERK-110).
    this.renderCue(view.recording ? view.cue : null, view.cueSecondsLeft);

    // The recognized song's synced lyrics (XERK-184): the same bordered card as a
    // cue, with "ARTIST — TITLE" over a window of lyric lines that scrolls as the
    // song plays. Shown only while a song is live and a session is recording — the
    // phone counterpart of the lens's lyric box and web/mobile's LiveLyricsBand.
    this.renderSong(view.recording ? view.song ?? null : null);

    const hasText =
      view.recording &&
      (view.segments.length > 0 || view.pastCues.length > 0 || view.partial !== "");
    this.els.text.hidden = !hasText;
    this.els.empty.hidden = hasText;
    if (!hasText) {
      // Idle explains how a session starts — from the button above once the
      // lens is wired (XERK-116), from the glasses either way; in-session it
      // says captions are coming.
      this.els.emptyTitle.textContent = view.recording ? "Listening for speech…" : "No session running";
      this.els.emptyHint.textContent = view.recording
        ? "Captions appear here as they are heard."
        : this.controls
          ? "Press Start, or tap your glasses, to begin a session."
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
          // A translated turn's original text is led by its source-language
          // chip (XERK-160) — the counterpart of the translation's "EN" tag.
          if (row.segment.translation && row.segment.lang) {
            li.append(
              this.make("span", "session-translation-lang", row.segment.lang.toUpperCase()),
              ` ${row.segment.text}`,
            );
          } else {
            li.textContent = row.segment.text;
          }
          // English translation of a non-English turn (XERK-160), turn-by-turn
          // under the original — the phone counterpart to the web's `.translation`.
          if (row.segment.translation) {
            const tr = doc.createElement("div");
            tr.className = "session-translation";
            const tag = this.make("span", "session-translation-lang", "EN");
            const text = this.make("span", "session-translation-text", row.segment.translation);
            tr.append(tag, text);
            li.appendChild(tr);
          }
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

  /**
   * Show exactly one of Start / Stop, matching the session state (XERK-116) —
   * the web Live panel's Record-or-Stop row. The whole row stays hidden until a
   * lens controller attaches its controls, so a browser-only run never offers a
   * button that can't do anything.
   */
  private renderControls(): void {
    this.els.controls.hidden = this.controls === null;
    this.els.start.hidden = this.recording;
    this.els.stop.hidden = !this.recording;
  }

  /**
   * Render (or hide) the live cue card: the title (accent) with the
   * countdown to its dismissal across from it (XERK-110), over its body.
   */
  private renderCue(cue: CueCard | null, secondsLeft?: number): void {
    if (!cue) {
      this.els.cue.hidden = true;
      this.els.cue.replaceChildren();
      this.countdown = null;
      return;
    }
    const doc = this.els.cue.ownerDocument;
    const head = doc.createElement("div");
    head.className = "session-cue-head";
    const title = doc.createElement("div");
    title.className = "session-cue-title";
    title.textContent = cue.title;
    const countdown = doc.createElement("div");
    countdown.className = "session-cue-countdown";
    // Out of the accessibility tree: the card is an aria-live region, and a
    // number changing every second would re-announce the whole cue each time.
    countdown.setAttribute("aria-hidden", "true");
    countdown.textContent = cueCountdownLabel(secondsLeft ?? 0);
    head.append(title, countdown);
    const body = doc.createElement("div");
    body.className = "session-cue-body";
    body.textContent = cue.body;
    const children: HTMLElement[] = [head, body];
    if (cue.source) {
      // Where the fact came from (XERK-120): the live source it was grounded in.
      const source = doc.createElement("div");
      source.className = "session-cue-source";
      source.textContent = cue.source;
      children.push(source);
    }
    this.els.cue.replaceChildren(...children);
    this.els.cue.hidden = false;
    this.countdown = countdown;
  }

  /**
   * Advance the live cue's countdown (XERK-110) without redrawing anything else.
   *
   * The lens ticker calls this several times a second while a cue is up; a full
   * `update()` at that rate would rebuild the transcript underneath it, fighting
   * text selection and the scroll position for a number that changes once a
   * second. No cue on screen → nothing to tick.
   */
  tickCue(secondsLeft: number): void {
    if (!this.countdown) return;
    this.countdown.textContent = cueCountdownLabel(secondsLeft);
  }

  /**
   * Render (or hide) the recognized-song lyric card (XERK-184): the title
   * "ARTIST — TITLE" with a ♪ badge across from it, over a window of lyric lines.
   * Rebuilt in full on each `update`; between updates `tickSong` advances just the
   * lyric lines, so the scroll keeps moving without redrawing the transcript.
   */
  private renderSong(song: LiveSong | null): void {
    this.song = song;
    if (!song) {
      this.els.song.hidden = true;
      this.els.song.replaceChildren();
      this.songLines = null;
      return;
    }
    const doc = this.els.song.ownerDocument;
    const head = doc.createElement("div");
    head.className = "session-song-head";
    const title = this.make("div", "session-song-title", `${song.artist} — ${song.title}`);
    const badge = this.make("div", "session-song-badge", "♪");
    // Out of the accessibility tree: the card is an aria-live region and the ♪ is
    // decoration, not content to announce.
    badge.setAttribute("aria-hidden", "true");
    head.append(title, badge);
    const body = doc.createElement("div");
    body.className = "session-song-body";
    this.els.song.replaceChildren(head, body);
    this.els.song.hidden = false;
    this.songLines = body;
    this.paintLyrics();
  }

  /**
   * Paint the current lyric window into the song card's body (XERK-184): the line
   * being sung now — `currentLyricIndex(song, Date.now())` off the local clock —
   * with one context line before and two upcoming, the current one highlighted.
   * An empty-lyrics song shows a quiet ♪ marker, matching web/mobile.
   */
  private paintLyrics(): void {
    if (!this.song || !this.songLines) return;
    const win = lyricWindow(this.song.lines, currentLyricIndex(this.song, Date.now()));
    const rows: HTMLElement[] =
      win.lines.length === 0
        ? [this.make("div", "session-song-line session-song-empty", "♪ ♪ ♪")]
        : win.lines.map((ln, i) =>
            this.make(
              "div",
              `session-song-line${i === win.currentIndex ? " current" : ""}`,
              ln.text || "♪",
            ),
          );
    this.songLines.replaceChildren(...rows);
  }

  /**
   * Advance the song's lyric scroll (XERK-184) without redrawing anything else —
   * the lens ticker calls this a few times a second while a song is up. Like
   * `tickCue`, a full `update()` at that rate would rebuild the transcript
   * underneath it. No song on screen → nothing to tick.
   */
  tickSong(): void {
    this.paintLyrics();
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

    // A container (not a bare <p>) so the source line can sit under the text
    // (XERK-120) and hide/show with it.
    const body = this.els.text.ownerDocument.createElement("div");
    body.className = "cue-inline-body";
    body.appendChild(this.make("p", "cue-inline-text", cue.body));
    if (cue.source) body.appendChild(this.make("p", "cue-inline-source", cue.source));
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
