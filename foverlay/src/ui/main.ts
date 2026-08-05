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
import { formatDuration, liveTranscriptRows, segmentTiming, sessionStatus, timeline } from "./lib";

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
  if (page === "history") void refreshHistory();
}

els.navSession.addEventListener("click", () => showPage("session"));
els.navHistory.addEventListener("click", () => showPage("history"));

// ---------------------------------------------------------------------------
// Session page (upstream phone/session.ts, simplified)
// ---------------------------------------------------------------------------

let live: TenirLiveState = {
  recording: false,
  connection: "closed",
  segments: [],
  partial: "",
  cues: [],
  song: null,
};
// Which reviewed cues are expanded, by cue id — survives the wholesale
// transcript rebuilds (upstream SessionPage.expanded).
const expandedCues = new Set<string>();
let wasRecording = false;

function buildCueRow(cue: TenirCue): HTMLElement {
  const open = expandedCues.has(cue.id);
  const li = document.createElement("li");
  const button = make("button", "cue-inline") as HTMLButtonElement;
  button.type = "button";
  const caret = make("span", "cue-inline-caret", open ? "▾" : "▸");
  const mark = make("span", "cue-inline-mark", "✦");
  const title = make("span", "cue-inline-title", cue.title);
  button.append(caret, mark, title);
  const body = make("div", "cue-inline-body");
  body.appendChild(make("p", "cue-inline-text", cue.body));
  if (cue.source) body.appendChild(make("p", "cue-inline-source", cue.source));
  body.hidden = !open;
  button.addEventListener("click", () => {
    const nowOpen = !expandedCues.has(cue.id);
    if (nowOpen) expandedCues.add(cue.id);
    else expandedCues.delete(cue.id);
    caret.textContent = nowOpen ? "▾" : "▸";
    body.hidden = !nowOpen;
  });
  li.append(button, body);
  return li;
}

function isPinnedToBottom(): boolean {
  const doc = document.scrollingElement;
  if (!doc) return true;
  return doc.scrollHeight - doc.scrollTop - doc.clientHeight <= 32;
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

  // The recognized song (XERK-184), phone-mirror form: title card only (the
  // synced-lyric scroll is not ported).
  if (live.recording && live.song) {
    els.song.replaceChildren(
      make("span", "song-badge", "♪"),
      document.createTextNode(`${live.song.title} — ${live.song.artist}`),
    );
    els.song.hidden = false;
  } else {
    els.song.hidden = true;
    els.song.replaceChildren();
  }

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
    expandedCues.clear();
  } else {
    const pinned = isPinnedToBottom();
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
        frag.appendChild(buildCueRow(row.cue));
      }
    }
    if (live.partial) {
      const li = document.createElement("li");
      li.className = "partial";
      li.textContent = live.partial;
      frag.appendChild(li);
    }
    els.text.replaceChildren(frag);
    // Follow the newest caption only while already at the bottom; a viewer who
    // scrolled up to re-read is left where they are.
    if (pinned) {
      const doc = document.scrollingElement;
      if (doc) doc.scrollTop = doc.scrollHeight;
    }
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
      // Inline expandable cue (the upstream modal is not ported).
      frag.appendChild(
        buildCueRow({
          id: item.cue.cueId,
          title: item.cue.title,
          body: item.cue.body,
          source: item.cue.source ?? undefined,
          afterIndex: -1,
        }),
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
