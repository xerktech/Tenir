/**
 * Tenir phone page — plain DOM, no framework (ported from the upstream Even
 * Hub app's `even/src/phone/{login,nav,session,history}.ts`, collapsed into
 * one WebView script).
 *
 * The upstream phone page shared a JS context with the lens app; here the
 * session state machine lives in the background JSContext and this page talks
 * to it over the typed `mentra` channel bus (src/shared/channels.ts):
 * snapshot/auth/live broadcasts in, login/logout/start/stop/fetch RPCs out.
 * All REST traffic (history) goes through the background's proxied-fetch RPC
 * — the WebView runs from file:// and can't fetch cross-origin itself.
 *
 * Not ported: audio playback/download in history (needs a browser streaming
 * the authenticated audio endpoint), and the cue-detail modal (cues expand
 * inline instead, like the session page's reviewed cues).
 */

import "../shared/channels";

import type { Conversation, ConversationSummary } from "../core/api";
import type {
  ProxyFetchRequest,
  TenirAuthState,
  TenirCue,
  TenirLiveState,
} from "../shared/types";
import {
  cueCountdownLabel,
  cueSecondsLeft,
  currentLyricIndex,
  lyricWindow,
} from "../core/live";
import {
  formatDuration,
  isPinnedToBottom,
  liveTranscriptRows,
  segmentTiming,
  sessionStatus,
  timeline,
} from "./lib";

// ---------------------------------------------------------------------------
// DOM handles
// ---------------------------------------------------------------------------

function byId<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (!el) throw new Error(`tenir ui: missing #${id}`);
  return el as T;
}

const els = {
  login: byId("login"),
  app: byId("app"),
  form: byId<HTMLFormElement>("login-form"),
  server: byId<HTMLInputElement>("server-url"),
  user: byId<HTMLInputElement>("username"),
  password: byId<HTMLInputElement>("password"),
  submit: byId<HTMLButtonElement>("login-submit"),
  error: byId("login-error"),
  signOut: byId<HTMLButtonElement>("sign-out"),
  openWeb: byId<HTMLButtonElement>("open-web"),
  appUser: byId("app-user"),
  toast: byId("app-toast"),
  // nav
  navSession: byId<HTMLButtonElement>("nav-session"),
  navHistory: byId<HTMLButtonElement>("nav-history"),
  pageSession: byId("page-session"),
  pageHistory: byId("page-history"),
  // session
  badge: byId("session-badge"),
  dot: byId("session-dot"),
  start: byId<HTMLButtonElement>("session-start"),
  stop: byId<HTMLButtonElement>("session-stop"),
  cue: byId("session-cue"),
  song: byId("session-song"),
  empty: byId("session-empty"),
  emptyTitle: byId("session-empty-title"),
  emptyHint: byId("session-empty-hint"),
  text: byId("session-text"),
  // history
  historyList: byId("history-list"),
  historySearch: byId<HTMLFormElement>("history-search"),
  historyQuery: byId<HTMLInputElement>("history-query"),
  historyStatus: byId("history-status"),
  historyRows: byId("history-rows"),
  historyDetail: byId("history-detail"),
  historyBack: byId<HTMLButtonElement>("history-back"),
  historyDelete: byId<HTMLButtonElement>("history-delete"),
  historyMeta: byId("history-meta"),
  cueToggle: byId("history-cue-toggle"),
  cueToggleInput: byId<HTMLInputElement>("history-cue-toggle-input"),
  historyTranscript: byId("history-transcript"),
};

function make(tag: string, className: string, text?: string): HTMLElement {
  const el = document.createElement(tag);
  el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

// ---------------------------------------------------------------------------
// Toast (upstream main.ts makeToast)
// ---------------------------------------------------------------------------

const TOAST_MS = 4000;
let toastTimer: ReturnType<typeof setTimeout> | null = null;
function toast(message: string): void {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.remove("show"), TOAST_MS);
}

// ---------------------------------------------------------------------------
// Proxied REST (all history traffic rides the background fetch RPC)
// ---------------------------------------------------------------------------

async function proxyFetch<T>(req: ProxyFetchRequest): Promise<T> {
  const res = await mentra.request("tenir:fetch", req);
  if (!res.ok) throw new Error(res.error);
  return res.data as T;
}

const historyApi = {
  list: (q?: string) => {
    const params = new URLSearchParams({ limit: "50", offset: "0" });
    if (q) params.set("q", q);
    return proxyFetch<ConversationSummary[]>({ path: `/conversations?${params.toString()}` });
  },
  get: (id: string) => proxyFetch<Conversation>({ path: `/conversations/${id}` }),
  remove: (id: string) => proxyFetch<void>({ path: `/conversations/${id}`, method: "DELETE" }),
};

// ---------------------------------------------------------------------------
// Auth view (upstream phone/login.ts)
// ---------------------------------------------------------------------------

let auth: TenirAuthState = { signedIn: false, username: "", serverUrl: "" };

function applyAuth(next: TenirAuthState): void {
  const wasSignedIn = auth.signedIn;
  auth = next;
  if (auth.serverUrl && !els.server.value) els.server.value = auth.serverUrl;
  if (auth.username && !els.user.value) els.user.value = auth.username;
  if (auth.signedIn) {
    els.appUser.textContent = auth.username;
    els.login.hidden = true;
    els.app.hidden = false;
    if (!wasSignedIn) showPage("session");
  } else {
    els.login.hidden = false;
    els.app.hidden = true;
    resetHistory();
  }
}

function showError(msg: string): void {
  els.error.textContent = msg;
  els.error.classList.add("show");
}

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  void (async () => {
    els.error.classList.remove("show");
    els.submit.disabled = true;
    els.submit.textContent = "Logging in…";
    try {
      const res = await mentra.request("tenir:login", {
        serverUrl: els.server.value,
        username: els.user.value.trim(),
        password: els.password.value,
      });
      if (res.ok) {
        els.password.value = "";
        // The background broadcasts tenir:auth too; apply eagerly regardless.
        applyAuth({ signedIn: true, username: res.username, serverUrl: els.server.value });
      } else {
        showError(res.error);
      }
    } catch (err) {
      showError(String(err instanceof Error ? err.message : err));
    } finally {
      els.submit.disabled = false;
      els.submit.textContent = "Log in";
    }
  })();
});

els.signOut.addEventListener("click", () => {
  void mentra.request("tenir:logout", {}).catch((err) => toast(String(err)));
});

els.openWeb.addEventListener("click", () => {
  // The server serves its own web UI at the https root of the same host.
  if (!auth.serverUrl) return;
  mentra.send("tenir:open-url", { url: `https://${auth.serverUrl}` });
});

// ---------------------------------------------------------------------------
// Bottom nav (upstream phone/nav.ts)
// ---------------------------------------------------------------------------

type Page = "session" | "history";
let currentPage: Page = "session";

function showPage(page: Page): void {
  currentPage = page;
  els.pageSession.hidden = page !== "session";
  els.pageHistory.hidden = page !== "history";
  els.navSession.classList.toggle("active", page === "session");
  els.navHistory.classList.toggle("active", page === "history");
  if (page === "session") els.navSession.setAttribute("aria-current", "page");
  else els.navSession.removeAttribute("aria-current");
  if (page === "history") els.navHistory.setAttribute("aria-current", "page");
  else els.navHistory.removeAttribute("aria-current");
  if (page === "history") void refreshHistory();
}

els.navSession.addEventListener("click", () => showPage("session"));
els.navHistory.addEventListener("click", () => showPage("history"));

// ---------------------------------------------------------------------------
// Session page (upstream phone/session.ts)
// ---------------------------------------------------------------------------

let live: TenirLiveState = {
  recording: false,
  connection: "closed",
  segments: [],
  partial: "",
  activeCue: null,
  cues: [],
  song: null,
};
// Which reviewed cues are expanded, by cue id — held PER SURFACE (upstream
// kept the session page's on the page and history used a modal), so expanding
// a cue in history can't pre-expand it in the live transcript and a live
// broadcast can't wipe the history view's state.
const sessionExpanded = new Set<string>();
const historyExpanded = new Set<string>();
let wasRecording = false;

function buildCueRow(cue: TenirCue, expanded: Set<string>): HTMLElement {
  const open = expanded.has(cue.id);
  const bodyId = `cue-body-${cue.id}`;
  const li = document.createElement("li");
  li.className = "session-cue-line";
  const button = make("button", "cue-inline") as HTMLButtonElement;
  button.type = "button";
  button.setAttribute("aria-expanded", String(open));
  button.setAttribute("aria-controls", bodyId);
  button.title = open ? "Hide cue detail" : "Show cue detail";
  const caret = make("span", "cue-inline-caret", open ? "▾" : "▸");
  caret.setAttribute("aria-hidden", "true");
  const mark = make("span", "cue-inline-mark", "✦");
  mark.setAttribute("aria-hidden", "true");
  const title = make("span", "cue-inline-title", cue.title);
  button.append(caret, mark, title);
  const body = make("div", "cue-inline-body");
  body.id = bodyId;
  body.appendChild(make("p", "cue-inline-text", cue.body));
  if (cue.source) body.appendChild(make("p", "cue-inline-source", cue.source));
  body.hidden = !open;
  button.addEventListener("click", () => {
    const nowOpen = !expanded.has(cue.id);
    if (nowOpen) expanded.add(cue.id);
    else expanded.delete(cue.id);
    button.setAttribute("aria-expanded", String(nowOpen));
    button.title = nowOpen ? "Hide cue detail" : "Show cue detail";
    caret.textContent = nowOpen ? "▾" : "▸";
    body.hidden = !nowOpen;
  });
  li.append(button, body);
  return li;
}

// ---- live cue card + song lyric card (XERK-81 / XERK-110 / XERK-184) -------
// Both cards tick between `tenir:live` broadcasts: the cue's countdown and the
// song's lyric window advance off the local clock (their anchors — shownAt /
// anchorAt — are wall-clock epochs stamped by the background). Targeted
// repaints only, so a tick never rebuilds the transcript underneath
// (upstream tickCue/tickSong).
const LIVE_TICK_MS = 250;
let cueCountdownEl: HTMLElement | null = null;
let songLinesEl: HTMLElement | null = null;
let liveTicker: ReturnType<typeof setInterval> | null = null;

function activeCueSeconds(): number {
  return cueSecondsLeft(live.activeCue ? Date.now() - live.activeCue.shownAt : 0);
}

/** The live cue card (XERK-81): title + countdown (XERK-110) over the body. */
function renderCueCard(): void {
  const cue = live.recording ? live.activeCue : null;
  if (!cue) {
    els.cue.hidden = true;
    els.cue.replaceChildren();
    cueCountdownEl = null;
    return;
  }
  const head = make("div", "session-cue-head");
  const title = make("div", "session-cue-title", cue.title);
  const countdown = make("div", "session-cue-countdown", cueCountdownLabel(activeCueSeconds()));
  // Out of the accessibility tree: the card is an aria-live region, and a
  // number changing every second would re-announce the whole cue each time.
  countdown.setAttribute("aria-hidden", "true");
  head.append(title, countdown);
  const body = make("div", "session-cue-body", cue.body);
  const children: HTMLElement[] = [head, body];
  if (cue.source) children.push(make("div", "session-cue-source", cue.source));
  els.cue.replaceChildren(...children);
  els.cue.hidden = false;
  cueCountdownEl = countdown;
}

/** Paint the current lyric window into the song card's body (XERK-184). */
function paintLyrics(): void {
  const song = live.recording ? live.song : null;
  if (!song || !songLinesEl) return;
  const win = lyricWindow(song.lines, currentLyricIndex(song, Date.now()));
  const rows: HTMLElement[] =
    win.lines.length === 0
      ? [make("div", "session-song-line session-song-empty", "♪ ♪ ♪")]
      : win.lines.map((ln, i) =>
          make(
            "div",
            `session-song-line${i === win.currentIndex ? " current" : ""}`,
            ln.text || "♪",
          ),
        );
  songLinesEl.replaceChildren(...rows);
}

/** The recognized-song lyric card (XERK-190): "TITLE — ARTIST" + ♪ over the window. */
function renderSongCard(): void {
  const song = live.recording ? live.song : null;
  if (!song) {
    els.song.hidden = true;
    els.song.replaceChildren();
    songLinesEl = null;
    return;
  }
  const head = make("div", "session-song-head");
  const title = make("div", "session-song-title", `${song.title} — ${song.artist}`);
  const badge = make("div", "session-song-badge", "♪");
  badge.setAttribute("aria-hidden", "true");
  head.append(title, badge);
  const body = make("div", "session-song-body");
  els.song.replaceChildren(head, body);
  els.song.hidden = false;
  songLinesEl = body;
  paintLyrics();
}

/** Run the 250ms card ticker exactly while a cue or song card is on screen. */
function syncLiveTicker(): void {
  const need = live.recording && (live.activeCue !== null || live.song !== null);
  if (need && !liveTicker) {
    liveTicker = setInterval(() => {
      if (cueCountdownEl && live.activeCue) {
        cueCountdownEl.textContent = cueCountdownLabel(activeCueSeconds());
      }
      paintLyrics();
    }, LIVE_TICK_MS);
  } else if (!need && liveTicker) {
    clearInterval(liveTicker);
    liveTicker = null;
  }
}

function renderSession(): void {
  const started = live.recording && !wasRecording;
  wasRecording = live.recording;

  els.dot.hidden = !live.recording;
  els.badge.textContent = live.recording ? sessionStatus(live.connection) : "idle";
  els.badge.className =
    live.recording && live.connection === "open" ? "badge-accent" : "badge-neutral";
  els.start.hidden = live.recording;
  els.stop.hidden = !live.recording;

  // The private context cue (XERK-81) and the recognized song's synced lyrics
  // (XERK-184): bordered cards pinned above the transcript, shown only while
  // live and recording — the phone counterparts of the lens popup box.
  renderCueCard();
  renderSongCard();
  syncLiveTicker();

  const hasText =
    live.recording && (live.segments.length > 0 || live.cues.length > 0 || live.partial !== "");
  els.text.hidden = !hasText;
  els.empty.hidden = hasText;
  if (!hasText) {
    els.emptyTitle.textContent = live.recording ? "Listening for speech…" : "No session running";
    els.emptyHint.textContent = live.recording
      ? "Captions appear here as they are heard."
      : "Press Start, or tap your glasses, to begin a session.";
    els.text.replaceChildren();
    // Nothing on screen to keep open; forget any stale expand state.
    sessionExpanded.clear();
  } else {
    // Was the viewer following the live feed (at the bottom of the
    // transcript's OWN scroll box, XERK-103) before this update?
    const pinned = isPinnedToBottom(els.text);
    const frag = document.createDocumentFragment();
    for (const row of liveTranscriptRows(live.segments, live.cues)) {
      if (row.kind === "segment") {
        const li = document.createElement("li");
        if (row.segment.translation && row.segment.lang) {
          li.append(
            make("span", "session-translation-lang", row.segment.lang.toUpperCase()),
            ` ${row.segment.text}`,
          );
        } else {
          li.textContent = row.segment.text;
        }
        if (row.segment.translation) {
          const tr = make("div", "session-translation");
          tr.append(
            make("span", "session-translation-lang", "EN"),
            make("span", "session-translation-text", row.segment.translation),
          );
          li.appendChild(tr);
        }
        frag.appendChild(li);
      } else {
        frag.appendChild(buildCueRow(row.cue, sessionExpanded));
      }
    }
    if (live.partial) {
      const li = document.createElement("li");
      li.className = "partial";
      li.textContent = live.partial;
      frag.appendChild(li);
    }
    els.text.replaceChildren(frag);
    // Follow the newest caption inside the transcript's own scroll box, so
    // the cue/song cards pinned above stay in view (XERK-103). Only stick
    // while already at the bottom; a viewer who scrolled up is left alone.
    if (pinned) els.text.scrollTop = els.text.scrollHeight;
  }

  // A session just started (possibly from the glasses): surface its live
  // transcript wherever the viewer was browsing.
  if (started) showPage("session");
}

els.start.addEventListener("click", () => {
  void mentra
    .request("tenir:start", {})
    .then((res) => {
      if (!res.ok && res.error) toast(res.error);
    })
    .catch((err) => toast(String(err)));
});

els.stop.addEventListener("click", () => {
  void mentra.request("tenir:stop", {}).catch((err) => toast(String(err)));
});

// ---------------------------------------------------------------------------
// History page (upstream phone/history.ts)
// ---------------------------------------------------------------------------

let currentConversation: Conversation | null = null;
let deleteArmed = false;
let disarmTimer: ReturnType<typeof setTimeout> | null = null;
let listReq = 0;

function emptyState(title: string, hint: string): HTMLElement {
  const box = make("div", "empty");
  box.appendChild(make("p", "empty-title", title));
  box.appendChild(make("p", "empty-hint", hint));
  return box;
}

function resetHistory(): void {
  listReq += 1;
  els.historyQuery.value = "";
  els.historyRows.replaceChildren();
  els.historyStatus.replaceChildren();
  showHistoryList();
}

async function refreshHistory(): Promise<void> {
  const req = ++listReq;
  const row = make("span", "spinner-row");
  const dot = make("span", "spinner");
  row.append(dot, "Loading…");
  els.historyStatus.replaceChildren(row);
  try {
    const rows = await historyApi.list(els.historyQuery.value.trim() || undefined);
    if (req !== listReq) return;
    renderHistoryRows(rows);
  } catch (err) {
    if (req !== listReq) return;
    els.historyRows.replaceChildren();
    const box = emptyState("Could not load history", String(err instanceof Error ? err.message : err));
    const retry = make("button", "btn btn-secondary", "Retry") as HTMLButtonElement;
    retry.type = "button";
    retry.addEventListener("click", () => void refreshHistory());
    els.historyStatus.replaceChildren(box, retry);
  }
}

function renderHistoryRows(rows: ConversationSummary[]): void {
  els.historyStatus.replaceChildren();
  els.historyRows.replaceChildren();
  if (rows.length === 0) {
    els.historyStatus.appendChild(
      emptyState("No conversations yet", "Captured conversations will appear here."),
    );
    return;
  }
  for (const c of rows) {
    const li = make("li", "history-item");
    const button = make("button", "history-open") as HTMLButtonElement;
    button.type = "button";
    button.appendChild(make("span", "history-when", new Date(c.startedAt).toLocaleString()));
    button.appendChild(
      make(
        "span",
        "history-meta",
        `${formatDuration(c.durationMs)} · ${c.segmentCount} turns · ${c.status}`,
      ),
    );
    button.addEventListener("click", () => void openHistoryDetail(c.id));
    li.appendChild(button);
    els.historyRows.appendChild(li);
  }
}

async function openHistoryDetail(id: string): Promise<void> {
  try {
    showHistoryDetail(await historyApi.get(id));
  } catch (err) {
    toast(String(err instanceof Error ? err.message : err));
  }
}

function showHistoryDetail(conv: Conversation): void {
  currentConversation = conv;
  disarmDelete();
  els.historyMeta.textContent = `${new Date(conv.startedAt).toLocaleString()} · ${formatDuration(
    conv.durationMs,
  )} · ${conv.segmentCount} turns`;
  const hasCues = (conv.cues?.length ?? 0) > 0;
  els.cueToggleInput.checked = true;
  els.cueToggle.hidden = !hasCues;
  renderHistoryTranscript(conv);
  els.historyList.hidden = true;
  els.historyDetail.hidden = false;
}

function renderHistoryTranscript(conv: Conversation): void {
  const cues = conv.cues ?? [];
  if (conv.segments.length === 0 && cues.length === 0) {
    els.historyTranscript.replaceChildren(
      make("p", "muted", "No transcript was recorded for this session."),
    );
    return;
  }
  const showCues = els.cueToggleInput.checked;
  const frag = document.createDocumentFragment();
  for (const item of timeline(conv)) {
    if (item.kind === "segment") {
      const row = make("div", "item");
      row.appendChild(make("span", "muted", segmentTiming(item.seg)));
      if (item.seg.translation && item.seg.lang) {
        row.append(" ");
        row.appendChild(make("span", "session-translation-lang", item.seg.lang.toUpperCase()));
      }
      row.append(` ${item.seg.text}`);
      if (item.seg.translation) {
        const tr = make("div", "session-translation");
        tr.appendChild(make("span", "session-translation-lang", "EN"));
        tr.appendChild(make("span", "session-translation-text", item.seg.translation));
        row.appendChild(tr);
      }
      frag.appendChild(row);
    } else if (showCues) {
      // Inline expandable cue (the upstream modal is not ported). History
      // keeps its own expand-state set, so live broadcasts can't disturb it.
      frag.appendChild(
        buildCueRow(
          {
            id: item.cue.cueId,
            title: item.cue.title,
            body: item.cue.body,
            source: item.cue.source ?? undefined,
            afterIndex: -1,
          },
          historyExpanded,
        ),
      );
    }
  }
  els.historyTranscript.replaceChildren(frag);
}

function showHistoryList(): void {
  currentConversation = null;
  disarmDelete();
  els.historyDetail.hidden = true;
  els.historyList.hidden = false;
}

function deleteClick(): void {
  if (!currentConversation) return;
  if (!deleteArmed) {
    deleteArmed = true;
    els.historyDelete.textContent = "Confirm delete";
    els.historyDelete.classList.add("armed");
    disarmTimer = setTimeout(() => disarmDelete(), 4000);
    return;
  }
  const id = currentConversation.id;
  disarmDelete();
  historyApi
    .remove(id)
    .then(() => {
      showHistoryList();
      void refreshHistory();
    })
    .catch((err) => toast(String(err instanceof Error ? err.message : err)));
}

function disarmDelete(): void {
  if (disarmTimer) {
    clearTimeout(disarmTimer);
    disarmTimer = null;
  }
  deleteArmed = false;
  els.historyDelete.textContent = "Delete";
  els.historyDelete.classList.remove("armed");
}

els.historySearch.addEventListener("submit", (e) => {
  e.preventDefault();
  void refreshHistory();
});
els.historyBack.addEventListener("click", () => showHistoryList());
els.historyDelete.addEventListener("click", () => deleteClick());
els.cueToggleInput.addEventListener("change", () => {
  if (currentConversation) renderHistoryTranscript(currentConversation);
});

// ---------------------------------------------------------------------------
// Channel wiring + bootstrap
// ---------------------------------------------------------------------------

mentra.on("tenir:snapshot", (snapshot) => {
  applyAuth(snapshot.auth);
  live = snapshot.live;
  renderSession();
});

mentra.on("tenir:auth", (state) => {
  applyAuth(state);
});

mentra.on("tenir:live", (state) => {
  live = state;
  renderSession();
});

renderSession();
mentra.ready();
