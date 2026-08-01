/**
 * The lens controller: the session state machine behind the glasses UI.
 *
 * Sessions are explicit (XERK-85): once signed in the lens idles ("tap to
 * start"); a single tap starts a new session. While one records, a single tap
 * does NOTHING (a brushed temple must not end a recording) — except while a
 * cue is up, when a tap or swipe resets the cue's auto-dismiss countdown
 * (XERK-129) without any further effect — a double tap
 * pops up a bordered full-width strip from the top of the screen (its own
 * container, added via `rebuildPageContainer`) with Continue (default, top) /
 * Exit session, padded above and below; everything the strip covers — status
 * line, clock, the first two caption rows — is blanked while it is up, and
 * the rest of the transcript keeps flowing below it: swiping moves the
 * highlight, a single tap confirms it, another double tap dismisses (same as
 * Continue). Exit session stops the
 * session (the api finalizes + stores it). Should the popup-page rebuild fail
 * on the host, the menu falls back into the caption band itself, so the
 * wearer is NEVER stranded inside a session. While recording the status line
 * reads "listening" with moving dots, the clock container shows the current
 * time, and the caption band holds only the rows that fit — nothing
 * overflows, and neither the band nor the clock ever captures input, so the
 * OS scroll animation only ever targets the invisible touch overlay.
 *
 * The glasses are not the only way in and out of a session (XERK-116): the
 * phone Session page gets the same start/stop through `setControls`, so a
 * wearer can begin or end a recording from whichever surface is to hand.
 *
 * Touch gestures reach the app on two channels: `sysEvent`, and `textEvent`
 * aimed at the captured touch overlay — both are routed through one gesture
 * handler (with a short same-gesture dedupe in case a host mirrors a gesture
 * on both channels).
 *
 * Lives apart from main.ts (the boot wiring) so the whole machine — clicks,
 * captions, ticker, persistence — runs under test with a stub bridge and a
 * fake api client (`deps.createClient`).
 */

import { OsEventTypeList, type EvenAppBridge, type EvenHubEvent } from "@evenrealities/even_hub_sdk";
import type { Song, SongDone, SongSync } from "@tenir/contract";

import {
  ApiClient,
  currentLyricIndex,
  cueSecondsLeft,
  langName,
  lyricWindow,
  type ApiHandlers,
  type LiveSong,
  type SessionParams,
} from "@tenir/client-core";

import { AudioCapture, pcmBytes } from "../audio/capture";
import { config } from "../config";
import { SessionPage, type PastCue } from "../phone/session";
import { silentLogin } from "../state/credentials";
import { SessionStore, type PersistedSession } from "../state/persist";
import { withBleTimeout, type KeyValueStorage } from "../state/storage";
import {
  CONTAINER,
  LensTextWriter,
  MENU_ROW_FIRST,
  MENU_ROW_LAST,
  buildCuePage,
  buildMainPage,
  buildMenuPage,
  clockText,
  cueBodyText,
  cueRowRange,
  cueTitleLine,
  dots,
  fitCaption,
  menuText,
  occludedCaption,
  SONG_BODY_LINES,
  songBody,
  songTitle,
  statusLine,
  tailCueBody,
  TRANSLATION_BODY_LINES,
  type CueCard,
  type MenuChoice,
  type PageContents,
} from "./layout";

// A cue box stays on the lens this long, then is auto-dismissed (XERK-81).
export const CUE_TTL_MS = 10000;
// Only one cue shows on the lens at a time (XERK-102): a cue arriving while
// another is up — or while the double-tap menu owns the popup — is queued and
// pops the moment the box frees. This bounds that backlog; over the cap the
// stalest waiting cue is dropped so the freshest still get their turn.
export const MAX_QUEUED_CUES = 8;
// A released cue drops into the phone transcript as a reviewable past cue
// (XERK-108); this bounds how many we keep so a long session can't grow them
// without limit. Over the cap the oldest falls off, matching MAX_SEGMENTS.
export const MAX_PAST_CUES = 60;

// Keep the on-lens transcript bounded; textContainerUpgrade caps at 2000 chars.
// fitCaption trims further to what the band can show — this only bounds the
// text we keep, persist, and measure.
const TRANSCRIPT_MAX_CHARS = 1200;
// Bound the translation box's accumulated body the same way (XERK-160): a long
// non-English run keeps appending turn translations; the box tails to a 5-row
// window (XERK-172, widened from four to five in XERK-176), so only the most
// recent text needs to be kept.
export const TRANSLATION_MAX_CHARS = 1200;
// The translation box's pinned title row (XERK-160). The box reuses the cue
// box's geometry — same place, same size — so the wearer reads one popup shape.
// The base verb, used alone as the title when the run's source language wasn't
// detected (XERK-173).
export const TRANSLATION_TITLE = "Translating";

// The translation box title (XERK-173): names the direction of the run —
// "Translating <Source> → English" once the spoken language is known, else the
// bare "Translating". English is always the target (the api translates
// non-English turns into English), so only the source varies. While the run is
// live a `tick` animates trailing dots — the same activity cue as the
// "listening…" status line (XERK-85) — so the title reads as work in progress;
// pass no tick once the run is done and the box is instead counting down.
export function translationTitle(sourceLang?: string, tick?: number): string {
  const from = langName(sourceLang);
  const base = from ? `Translating ${from} → English` : TRANSLATION_TITLE;
  return tick == null ? base : `${base}${dots(tick)}`;
}
// Cap how many finalized turns we keep on the lens.
const MAX_SEGMENTS = 60;
// The activity ticker (XERK-85): moves the "listening" dots and keeps the
// clock current. Writes are deduped in LensTextWriter, so only frames that
// actually changed cost a BLE round-trip.
export const TICK_MS = 600;
// A host may deliver the same physical gesture on both the sysEvent and the
// textEvent channel; a same-type gesture repeating inside this window is the
// mirror, not a second gesture. (Two intentional taps this close together are
// a double tap and arrive as one DOUBLE_CLICK anyway.)
export const GESTURE_DEDUPE_MS = 200;

// The touch gestures routed through the dedupe (system lifecycle events are not).
const TOUCH_GESTURES: ReadonlySet<OsEventTypeList> = new Set([
  OsEventTypeList.CLICK_EVENT,
  OsEventTypeList.DOUBLE_CLICK_EVENT,
  OsEventTypeList.SCROLL_TOP_EVENT,
  OsEventTypeList.SCROLL_BOTTOM_EVENT,
]);

export const SIGN_IN_PROMPT = "Not signed in — open the Tenir app on your phone to sign in.";
export const IDLE_PROMPT = "Tap to start a new session.";

/** One finalized turn on the lens, keyed so a translation can pair to it (XERK-160).
 * `lang` is the turn's detected spoken language — the phone mirror tags a
 * translated turn's original text with it, pairing with the "EN" tag. */
type LensSegment = { id: string; text: string; lang?: string; translation?: string };

type Mutable = {
  sessionId?: string; // authoritative id, persisted so a resume survives backgrounding
  micSource: PersistedSession["micSource"];
  segments: LensSegment[]; // finalized turns
  partial: string; // current live hypothesis
  connection: "connecting" | "open" | "closed";
  recording: boolean; // a session is running (XERK-85: tap starts, popup exits)
  menu: MenuChoice | null; // the in-session popup's highlight; null = closed
  // The active/queued cues carry a transcript anchor (afterIndex, XERK-108) fixed
  // when they arrive, so a released one lands after the turn that triggered it.
  cue: PastCue | null; // the private context cue currently on the lens (XERK-81)
  cueQueue: PastCue[]; // cues waiting behind the active one (FIFO, XERK-102)
  pastCues: PastCue[]; // released cues embedded in the phone transcript (XERK-108)
  // The live translation run (XERK-160): English renderings of the non-English
  // turns currently being spoken, shown in the cue box's slot. Turns stack into
  // the same box for the length of a run (XERK-181). `done` flips when the api
  // says the other language is done being spoken (`translation.done`); the box
  // is then dismissed at once — the server already held ~translation_hold_ms of
  // silence, so there is no on-lens countdown to wait out (XERK-181). `done`
  // therefore only ever lingers true while a finished run waits behind a box that
  // outranks it — the open menu or a live song (XERK-194) — which dismisses it
  // when the box frees (closeMenu / dismissSong). `sourceLang` is the run's detected
  // spoken language, fixed from its first turn, so the box title can read
  // "<Source> → English" (XERK-173).
  translation: { texts: string[]; done: boolean; sourceLang?: string } | null;
  // The song currently recognized playing (XERK-184): its time-synced lyrics
  // auto-scroll in the cue box's slot, exactly like a translation run reuses it.
  // The scroll is client-driven from the anchor stamped on the LiveSong: `song`
  // (re)opens it, `song.sync` re-anchors to correct drift, `song.done` clears it.
  // A live song owns the box over a translation run and a cue (XERK-194: on the
  // glasses lyrics beat translation beat cues; the api suppresses cues while it
  // plays), yielding only to the interactive menu (see the precedence in
  // `rebuildPage`). `null` when nothing is playing.
  song: LiveSong | null;
};

/** The slice of ApiClient the controller drives — structural, so tests pass a fake. */
export interface CaptureClient {
  start(params: SessionParams, resumeSessionId?: string): void;
  stop(): void;
  sendAudio(pcm: Uint8Array): boolean;
}

export interface LensDeps {
  /** Api client factory; tests inject a fake to drive captions without a socket. */
  createClient?: (url: string, handlers: ApiHandlers) => CaptureClient;
}

/** What the phone login page drives on the lens side. */
export interface LensControls {
  /** Signed in: resume a persisted mid-session recording, else idle at "tap to start". */
  enable(): void;
  /** Signed out: stop any session and show the sign-in prompt. */
  disable(): void;
}

/**
 * Wire the lens: session restore, audio capture and event routing. Returns the
 * enable/disable pair the phone login drives; until then the lens keeps its
 * boot text ("starting…") and the phone page resolves the auth state within a
 * moment of this returning.
 */
export async function wireLens(
  bridge: EvenAppBridge,
  storage: KeyValueStorage,
  writer: LensTextWriter,
  sessionPage: SessionPage | null,
  deps: LensDeps = {},
): Promise<LensControls> {
  const createClient = deps.createClient ?? ((url, handlers) => new ApiClient(url, handlers));
  const store = new SessionStore(bridge);

  // A persisted session means recording was in progress when the app last went
  // away. It is resumed on the first enable() ONLY if it was backgrounded, not
  // closed (XERK-117): a snapshot left by a close/kill is dropped — its server
  // session has been (or, via the resume grace window, will be) finalized to
  // history — so the app idles at "tap to start" instead of reopening it.
  const loaded = await store.load(); // timeout-bounded (persist.ts)
  let pendingResume: PersistedSession | null = loaded?.resumable ? loaded : null;
  // Forget a non-resumable remnant so it can't be mistaken for live state later.
  if (loaded && !loaded.resumable) void store.clear();

  const state: Mutable = {
    micSource: pendingResume?.micSource ?? config.defaultMicSource,
    segments: [],
    partial: "",
    connection: "closed",
    recording: false,
    menu: null,
    cue: null,
    cueQueue: [],
    pastCues: [],
    translation: null,
    song: null,
  };
  let cueTimer: ReturnType<typeof setTimeout> | null = null;
  // When the cue currently in the box went up, so its countdown (XERK-110) can
  // be derived on every tick rather than decremented — the ticker idles while
  // the lens is backgrounded, and a derived count comes back correct.
  let cueShownAt: number | null = null;
  // The translation box no longer auto-dismisses on a timer (XERK-181): a run is
  // shown until the api declares it done, at which point the box is dismissed
  // straight away — the server's silence hold is the read time, so there is no
  // redundant on-lens countdown to arm or derive.
  let enabled = false; // signed in — clicks act only while enabled
  let tick = 0;
  // Whether the app is currently backgrounded (XERK-117): set on FOREGROUND_EXIT,
  // cleared on FOREGROUND_ENTER. It tags each persisted snapshot as resumable only
  // while backgrounded, so a session persisted in the foreground and then killed
  // is not resumed on the next boot — it ends and saves to history instead.
  let backgrounded = false;
  // The popup-page rebuild failed on the host: the menu renders inside the
  // caption band instead, so the wearer always has a way out of a session.
  let menuFallback = false;

  // ---- lens rendering helpers ------------------------------------------------
  const transcriptText = () => state.segments.map((s) => s.text).join("\n");
  // The translation box's content as a cue-shaped card (XERK-160): it reuses the
  // cue box's layout wholesale — same place, same size — with the run's turn
  // translations stacked as its body. Unlike a cue (read from the top, its
  // overflow host-scrolled), the box streams turn after turn, so its body is
  // tailed to the last rows (XERK-172): a new turn arrives at the bottom, like
  // the caption band, instead of the rebuild snapping the host scroll to the top.
  const translationCard = (): CueCard => ({
    // Always animate the "Translating…" dots (XERK-173): the box is only ever
    // visible while a run is live — a finished run is dismissed at once
    // (XERK-181), never left on the lens counting down — so the title never
    // needs a static, done form.
    title: translationTitle(state.translation?.sourceLang, tick),
    body: tailCueBody((state.translation?.texts ?? []).join("\n"), TRANSLATION_BODY_LINES),
  });
  // The song box as a cue-shaped card (XERK-184): it reuses the cue box's layout
  // wholesale — same place, same size — with the recognized song's lyrics as its
  // body. The body is the auto-scroll WINDOW around the line being sung now:
  // `currentLyricIndex(song, Date.now())` off the local clock, then `lyricWindow`
  // (LYRIC_LINES_BEFORE context line, the current line, LYRIC_LINES_AFTER upcoming
  // — SONG_BODY_LINES rows). The current line sits `before` rows from the top —
  // 2nd from the top mid-song, as on web/mobile — and is marked "> " (XERK-189),
  // the lens stand-in for the bold highlight the HUD can't render (`songBody`).
  // As the clock advances the window slides and the lyrics scroll (repainted from
  // the ticker, see `renderSongBody`). An empty-lyrics song shows the title over a
  // quiet ♪ marker, matching web/mobile. The title is "SONG NAME — ARTIST".
  const songCard = (): CueCard => {
    const song = state.song!;
    const win = lyricWindow(song.lines, currentLyricIndex(song, Date.now()));
    return { title: songTitle(song.artist, song.title), body: songBody(win) };
  };
  /** The caption band's live text: full band, or masked under the popup box. */
  const liveCaption = () => {
    // The popup-page rebuild failed: the band itself carries the menu, so the
    // wearer can still exit the session.
    if (state.menu && menuFallback) return menuText(state.menu);
    const body = transcriptText();
    const full = state.partial ? `${body}${body ? "\n" : ""}${state.partial}` : body;
    // Only the rows that FIT (XERK-85): nothing overflows, so the host has
    // nothing to scroll; old text simply falls off the top. While the popup is
    // up (menu, translation or cue), the rows its box covers are masked — an
    // opaque popup hides exactly those — and the rows around it keep flowing.
    const bounded = full.slice(-TRANSCRIPT_MAX_CHARS);
    if (!popupUp()) return fitCaption(bounded);
    // Mask exactly the rows the popup that's up covers: the cue/translation box
    // (XERK-112) hides more rows than the menu, and only as many as it is tall
    // (XERK-119) — a short box frees the transcript rows below it. The masked
    // range follows the box on top: menu > song > translation > cue (XERK-194).
    const [first, last] = state.menu
      ? [MENU_ROW_FIRST, MENU_ROW_LAST]
      : state.song
        ? cueRowRange(songCard(), SONG_BODY_LINES)
        : state.translation
          ? cueRowRange(translationCard(), TRANSLATION_BODY_LINES)
          : state.cue
            ? cueRowRange(state.cue)
            : [MENU_ROW_FIRST, MENU_ROW_LAST];
    return occludedCaption(bounded, first, last);
  };
  // A popup strip is up: the interactive menu, a live translation box (XERK-160),
  // a recognized song's lyric box (XERK-184), or a private-context cue box
  // (XERK-81). All live in the same bordered container across the top.
  const popupUp = () =>
    state.menu !== null ||
    state.translation !== null ||
    state.song !== null ||
    state.cue !== null;
  // The popup strip covers the status/clock line and the first caption row:
  // whatever it covers is blanked while it is up (fallback mode has no strip).
  const popupCovering = () => popupUp() && !menuFallback;
  const statusContent = () =>
    !enabled ? "not signed in" : popupCovering() ? "" : statusLine(state, tick);
  const clockContent = () => (enabled && !popupCovering() ? clockText(new Date()) : "");
  /** What every container should currently read — the one source of page truth. */
  const pageContents = (): PageContents => ({
    status: statusContent(),
    caption: !enabled ? SIGN_IN_PROMPT : state.recording ? liveCaption() : IDLE_PROMPT,
    clock: clockContent(),
  });
  const renderCaption = () => writer.set(CONTAINER.caption, pageContents().caption);
  // Seconds left before the cue in the box is auto-dismissed (XERK-110), for
  // the countdown at the right end of its title row.
  const cueCountdown = () => cueSecondsLeft(cueShownAt == null ? 0 : Date.now() - cueShownAt);
  // Repaint the shared popup box: the menu highlight, or the cue's pinned title
  // row (which carries its countdown, so this is also how the countdown advances
  // — the writer drops the frame whenever the second hasn't turned over). Only
  // the title is repainted for a cue; its body sits in its own container the
  // host scrolls (XERK-133), left untouched so a countdown tick can't reset it.
  const renderMenu = () => {
    if (menuFallback) return;
    // Repaint the title row of whichever box is on top (XERK-194): song beats
    // translation beats cue, the menu above all.
    if (state.menu) writer.set(CONTAINER.menu, menuText(state.menu));
    else if (state.song) writer.set(CONTAINER.menu, cueTitleLine(songCard()));
    else if (state.translation)
      writer.set(CONTAINER.menu, cueTitleLine(translationCard()));
    else if (state.cue) writer.set(CONTAINER.menu, cueTitleLine(state.cue, cueCountdown()));
  };
  // Repaint the scrolling body of the song box (XERK-184). Unlike a cue (written
  // once, its overflow host-scrolled) or a translation (rewritten only on a new
  // turn), a song's lyric window advances on its OWN off the local clock, so the
  // ticker repaints this container as the current line moves — the writer drops
  // the frame whenever the window hasn't turned over. Nothing to paint unless the
  // song box is the one actually on screen — only the menu outranks it now
  // (XERK-194: the song beats a translation for the box).
  const renderSongBody = () => {
    if (menuFallback || state.menu || !state.song) return;
    writer.set(CONTAINER.cueBody, cueBodyText(songCard()));
  };
  const renderStatus = () => writer.set(CONTAINER.status, statusContent());
  // The clock shows whenever signed in — on the idle "ready" page and while
  // recording alike (XERK-85 feedback) — except under the popup strip.
  const renderClock = () => writer.set(CONTAINER.clock, clockContent());
  const syncPhone = () =>
    sessionPage?.update({
      recording: state.recording,
      connection: state.connection,
      segments: state.segments,
      partial: state.partial,
      cue: state.cue,
      cueSecondsLeft: cueCountdown(),
      pastCues: state.pastCues,
      song: state.song,
    });

  /**
   * Swap the page between the plain layout and the one with the popup box —
   * `rebuildPageContainer` is the SDK's sanctioned runtime page change. Rides
   * the writer's serialized lane, then re-asserts every container's text so a
   * stale queued write from just before the swap can't land on the new page.
   */
  const rebuildPage = () => {
    const contents = pageContents();
    // The menu outranks everything (it is interactive). On the glasses lyrics beat
    // translation beat cues (XERK-194): the single popup box can hold only one, so a
    // recognized song owns it over a live translation run, which owns it over a cue
    // (XERK-160: cues neither trigger nor appear mid-run). Web/mobile/phone have room
    // to show lyrics and a translation at once, so they render both — only the lens
    // arbitrates. The translation box and the song box each ARE the cue box — same
    // page shape, same geometry (the song showing SONG_BODY_LINES lyric rows).
    // Precedence: menu > song > translation > cue > main.
    const page = state.menu
      ? buildMenuPage(contents, state.menu)
      : state.song
        ? buildCuePage(contents, songCard(), undefined, SONG_BODY_LINES)
        : state.translation
          ? buildCuePage(contents, translationCard(), undefined, TRANSLATION_BODY_LINES)
          : state.cue
            ? buildCuePage(contents, state.cue, cueCountdown())
            : buildMainPage(contents);
    const openingMenu = state.menu !== null;
    const openingSong = state.menu === null && state.song !== null;
    const openingTranslation =
      state.menu === null && state.song === null && state.translation !== null;
    const openingCue =
      state.menu === null &&
      state.song === null &&
      state.translation === null &&
      state.cue !== null;
    writer.run(async () => {
      const ok = await withBleTimeout(bridge.rebuildPageContainer(page), false);
      if (!ok && openingMenu && state.menu && !menuFallback) {
        // The popup page never appeared (XERK-85: this once stranded the
        // wearer inside a session). Fall back: render the menu inside the
        // caption band, which needs no rebuild at all.
        menuFallback = true;
        writer.set(CONTAINER.caption, menuText(state.menu));
      } else if (!ok && openingTranslation && state.translation) {
        // Like a cue, the on-lens box is a best-effort aside: drop it rather
        // than leave the band masked with no box. The translations themselves
        // are safe — they're paired to their turns on the phone mirror.
        state.translation = null;
        writer.set(CONTAINER.caption, pageContents().caption);
      } else if (!ok && openingSong && state.song) {
        // The song box is a best-effort aside too (XERK-184): if its popup page
        // never appeared, drop it rather than leave the caption band masked with
        // no box. The song keeps playing; a later song.sync re-opens the box.
        state.song = null;
        writer.set(CONTAINER.caption, pageContents().caption);
      } else if (!ok && openingCue && state.cue) {
        // A cue is a best-effort aside — if its popup page never appeared,
        // drop it rather than leave the caption band masked with no box.
        state.cue = null;
        clearCueTimer();
        writer.set(CONTAINER.caption, pageContents().caption);
      }
    });
    writer.invalidate();
    writer.set(CONTAINER.status, contents.status);
    writer.set(CONTAINER.caption, contents.caption);
    writer.set(CONTAINER.clock, contents.clock);
    renderMenu();
  };

  const showIdle = () => {
    renderStatus();
    renderClock();
    writer.set(CONTAINER.caption, IDLE_PROMPT);
  };

  const showSignInPrompt = () => {
    renderStatus();
    renderClock();
    writer.set(CONTAINER.caption, SIGN_IN_PROMPT);
  };

  const persist = () =>
    store.save({
      sessionId: state.sessionId, // persisted so a resume survives the WebView migration
      micSource: state.micSource,
      transcript: transcriptText().slice(-TRANSCRIPT_MAX_CHARS),
      // Resumable only while backgrounded (XERK-117): a snapshot written in the
      // foreground is not restored after a close/kill.
      resumable: backgrounded,
    });

  // Api client + capture, connected only while a session records.
  let client: CaptureClient | null = null;
  const capture = new AudioCapture(bridge);
  // One silent re-login per unauthorized rejection, so an expired token heals
  // itself without looping against a server that keeps saying no.
  let reauthAttempted = false;

  const connect = () => {
    // Reconnects (e.g. after a re-login) replace the previous client; the
    // session id is kept so the api resumes the same conversation.
    client?.stop();
    reauthAttempted = false;
    state.connection = "connecting";
    client = createClient(config.apiWsUrl, {
      onConnectionChange: (s) => {
        state.connection = s;
        renderStatus();
        syncPhone();
      },
      onReady: (m) => {
        // Capture the authoritative id and persist it so a later restore can resume
        // this same session.
        state.sessionId = m.sessionId;
        reauthAttempted = false;
        persist();
        renderStatus();
      },
      onPartial: (m) => {
        state.partial = m.text;
        renderCaption();
        syncPhone();
      },
      onFinal: (m) => {
        state.segments.push({ id: m.segmentId, text: m.text, lang: m.lang });
        if (state.segments.length > MAX_SEGMENTS) {
          state.segments.shift();
          // The oldest turn fell off the bounded window: shift every embedded
          // cue's anchor to match, so each stays after the same words (or leads
          // the transcript once its anchor turn is gone) (XERK-108).
          for (const pc of state.pastCues) pc.afterIndex -= 1;
        }
        state.partial = "";
        renderCaption();
        syncPhone();
        persist();
      },
      onCue: (m) => showCue({ id: m.cueId, title: m.title, body: m.body, source: m.source }),
      onTranslation: (m) => showTranslation(m.segmentId, m.text, m.sourceLang),
      onTranslationDone: () => finishTranslation(),
      onSong: (m) => showSong(m),
      onSongSync: (m) => syncSong(m),
      onSongDone: (m) => finishSong(m),
      onError: (m) => {
        console.warn("api error", m.code, m.message);
        if (m.code === "unauthorized") {
          // Expired/revoked token: re-login silently with the cached credentials
          // and reconnect. Only if that fails does the wearer get sent to the phone.
          if (!reauthAttempted) {
            reauthAttempted = true;
            void silentLogin(storage).then((principal) => {
              if (principal) connect();
              else disable();
            });
          } else {
            disable();
          }
        }
      },
    });
    renderStatus();
    renderCaption();
    renderClock();
    void capture.start();
    client.start(
      { micSource: state.micSource, sourceLang: config.defaultSourceLang },
      state.sessionId, // resume the prior session if we restored one
    );
  };

  /** Start a session — fresh, or resuming the persisted one after backgrounding. */
  const startSession = (resume?: PersistedSession) => {
    state.recording = true;
    state.sessionId = resume?.sessionId;
    state.micSource = resume?.micSource ?? config.defaultMicSource;
    // A resumed transcript comes back as a single restored block.
    state.segments = resume?.transcript ? [{ id: "restored", text: resume.transcript }] : [];
    state.partial = "";
    // A new session reviews its own cues from scratch — the prior session's
    // embedded past cues don't carry over (XERK-108).
    state.pastCues = [];
    state.translation = null;
    state.song = null;
    connect();
    syncPhone();
  };

  /** Stop the current session: the api finalizes + stores it; the lens idles. */
  const stopSession = () => {
    const menuWasOpen = state.menu !== null;
    state.recording = false;
    state.menu = null;
    state.cue = null;
    state.cueQueue = [];
    state.pastCues = []; // the transcript is cleared on stop, so the review cues go with it
    state.translation = null;
    state.song = null;
    clearCueTimer();
    menuFallback = false;
    client?.stop(); // sends session.end, closes, no reconnect
    client = null;
    void capture.stop();
    state.connection = "closed";
    state.sessionId = undefined;
    state.segments = [];
    state.partial = "";
    void store.clear(); // the session is over — nothing to resume anymore
    // Leaving via the popup: rebuild back to the plain page (which also
    // carries the idle texts); otherwise plain idle writes suffice.
    if (menuWasOpen) rebuildPage();
    else showIdle();
    syncPhone();
  };

  // Start/stop from the phone Session page (XERK-116): the same two transitions
  // the glasses drive (a tap to start, the popup's Exit session to stop), so a
  // session no longer has to be begun and ended on the glasses. Both are guarded
  // by the current state — the buttons and the lens can't disagree about whether
  // a session is running, but a queued tap racing a gesture still must not start
  // a second one or stop a session twice.
  sessionPage?.setControls({
    start: () => {
      if (!enabled || state.recording) return;
      startSession();
    },
    stop: () => {
      if (!state.recording) return;
      stopSession();
    },
  });

  const enable = () => {
    enabled = true;
    if (pendingResume) {
      const resume = pendingResume;
      pendingResume = null;
      startSession(resume);
    } else if (!state.recording) {
      showIdle();
      syncPhone(); // the phone Session page shows its idle state (XERK-93)
    }
  };

  const disable = () => {
    enabled = false;
    if (state.recording) stopSession();
    showSignInPrompt();
    syncPhone();
  };

  // The activity ticker (XERK-85): while signed in, keep the top-right clock on
  // the current minute (idle "ready" page included), move the "listening" dots
  // while recording, and advance a live cue's countdown (XERK-110) on both
  // surfaces it shows on.
  //
  // None of this is gated on the phone app being foregrounded (XERK-113): the
  // lens is what the wearer reads, and the glasses keep showing it over the BLE
  // link while the phone app is backgrounded. So the clock, the dots, and the
  // cue count all have to stay live there — just as the cue's auto-dismiss timer
  // does. Gating them once left a backgrounded cue frozen at "10s", the clock
  // stuck on a stale minute, and the dots stalled, until the app was next
  // foregrounded. TICK_MS is well under a second, so the cue count never lags
  // the release timer by more than a tick; the writer drops unchanged frames, so
  // this still costs BLE only when text actually changes; and the phone gets a
  // targeted text update rather than a transcript rebuild.
  const ticker = setInterval(() => {
    if (!enabled) return;
    tick += 1;
    renderClock();
    if (state.recording && state.connection === "open") renderStatus();
    // Advance whichever box owns the lens, in precedence order (XERK-194: song
    // beats translation beats cue). A song and a translation can be live at once
    // — the song owns the box, so its lyrics scroll while the held translation
    // waits; the phone mirror still ticks the song alongside the inline translation.
    if (state.song) {
      // A live song's lyrics auto-scroll off the local clock (XERK-184): repaint
      // the body window so the current line advances as the song plays, on both
      // surfaces it shows on. The writer drops the frame whenever the window
      // hasn't turned over, so this costs BLE only when the lyric line changes.
      renderSongBody();
      sessionPage?.tickSong();
    } else if (state.translation) {
      // A live run animates the "Translating…" dots (XERK-173): only the title
      // row repaints, and the writer drops the frame when nothing changed. A
      // finished run never lingers here — it is dismissed at once (XERK-181) —
      // so the box the ticker sees is always live and always animating.
      renderMenu();
    } else if (state.cue) {
      renderMenu();
      sessionPage?.tickCue(cueCountdown());
    }
  }, TICK_MS);

  // The single event subscription (audio + gestures + system events). Touch
  // gestures arrive on the sysEvent channel or as textEvent aimed at the
  // captured touch overlay — on-device swipes come as the latter (XERK-85
  // feedback) — so both feed one handler, deduped per gesture type in case a
  // host mirrors a gesture on both channels.
  let lastGesture = { type: -1 as OsEventTypeList | -1, at: 0 };
  const off = bridge.onEvenHubEvent((event: EvenHubEvent) => {
    if (event.audioEvent) {
      if (state.recording) client?.sendAudio(pcmBytes(event.audioEvent));
      return;
    }
    const payload = event.sysEvent ?? event.textEvent;
    if (!payload) return;
    const type = payload.eventType ?? OsEventTypeList.CLICK_EVENT; // zero-omission
    if (TOUCH_GESTURES.has(type)) {
      const now = Date.now();
      if (type === lastGesture.type && now - lastGesture.at < GESTURE_DEDUPE_MS) return;
      lastGesture = { type, at: now };
    }
    handleGesture(type);
  });

  // The app is closing for real (SYSTEM_EXIT/ABNORMAL_EXIT), not just
  // backgrounding. End any running session — client.stop() sends session.end so
  // the api finalizes and stores it to history (and even if that frame never
  // makes it out on an abnormal exit, the dropped socket is finalized once the
  // resume grace window lapses) — and CLEAR the persisted snapshot so the next
  // boot does not reopen this session (XERK-117). This is why a close must be
  // distinguished from a background flush, which keeps the session resumable.
  const cleanup = async () => {
    clearInterval(ticker);
    if (cueTimer) clearTimeout(cueTimer);
    off();
    await capture.stop();
    client?.stop();
    await store.clear();
  };

  const startCueTimer = () => {
    if (cueTimer) clearTimeout(cueTimer);
    // The countdown (XERK-110) runs off the same instant the release timer does.
    cueShownAt = Date.now();
    cueTimer = setTimeout(() => dismissCue(), CUE_TTL_MS);
  };

  /** Cancel the pending auto-dismiss, and with it the countdown behind it. */
  const clearCueTimer = () => {
    if (cueTimer) clearTimeout(cueTimer);
    cueTimer = null;
    cueShownAt = null;
  };

  /**
   * An English translation of one finalized non-English turn arrived (XERK-160).
   * Two jobs: pair it with its turn for the phone mirror, and (unless a song owns
   * the box) put it in the on-lens box — the cue box's slot, place and size (the
   * ticket's contract). Every turn of a live run appends, stacking into the SAME
   * box (XERK-181; the box tails to its newest rows, XERK-172); a fresh run starts
   * only once the prior one is gone (a done run is dismissed at once, XERK-181, so
   * the only "done" run still around is one held behind the open menu or a song).
   * A cue holding the box loses it immediately — a translation is time-sensitive
   * and always overwrites a cue's countdown (XERK-181) — and the cue is embedded
   * for review rather than dropped. A recognized song outranks the translation on
   * the lens, though (XERK-194: lyrics beat translation): while one plays the run
   * still accumulates for the phone mirror and takes the box only once the song
   * ends, so a translation never overwrites the lyrics box.
   */
  const showTranslation = (segmentId: string, text: string, sourceLang?: string) => {
    const seg = state.segments.find((s) => s.id === segmentId);
    if (seg) seg.translation = text;
    if (!state.translation || state.translation.done) {
      // A new run: its source language, fixed here from the first turn, titles
      // the box "<Source> → English" (XERK-173). A cue can only be showing when no
      // song owns the box, so this embed never displaces a song.
      state.translation = { texts: [text], done: false, sourceLang };
      if (state.cue) {
        embedPastCue(state.cue);
        state.cue = null;
        clearCueTimer();
      }
    } else {
      state.translation.texts.push(text);
      // Adopt a later turn's source language if the run started without one, so
      // a first turn that arrived untagged doesn't strand the box on the generic
      // title (XERK-173).
      if (!state.translation.sourceLang && sourceLang) state.translation.sourceLang = sourceLang;
      // Bound the accumulated body like the transcript: the box tails to its
      // newest rows (XERK-172), so only the most recent text needs keeping.
      while (
        state.translation.texts.length > 1 &&
        state.translation.texts.join("\n").length > TRANSLATION_MAX_CHARS
      ) {
        state.translation.texts.shift();
      }
    }
    // The menu and a live song each outrank the box: a run behind either keeps
    // accumulating and shows only when the box frees (closeMenu / dismissSong).
    if (!state.menu && !state.song) rebuildPage();
    syncPhone();
  };

  /**
   * The api says the other language is done being spoken (`translation.done`,
   * XERK-160): the run is over, so the box is dismissed at once (XERK-181). The
   * server already held ~translation_hold_ms of silence before sending this —
   * that is the read time — so there is no on-lens countdown to add. Behind the
   * open menu OR a live song (XERK-194) the finished run waits; `closeMenu` /
   * `dismissSong` dismiss it when the box would otherwise become visible.
   */
  const finishTranslation = () => {
    if (!state.translation || state.translation.done) return;
    state.translation.done = true;
    if (!state.menu && !state.song) dismissTranslation();
  };

  /** The translation box leaves; queued cues resume now the run is over (XERK-160). */
  const dismissTranslation = () => {
    if (!state.translation) return;
    state.translation = null;
    // The translation only holds the box when neither the menu nor a live song is
    // above it (XERK-194: song > translation): only then does its leaving free the
    // box, so drain a queued cue up and repaint. Behind either, the run was hidden
    // and there is nothing on screen to change.
    if (!state.menu && !state.song) {
      state.cue = state.cueQueue.shift() ?? null;
      if (state.cue) startCueTimer();
      rebuildPage();
    }
    syncPhone();
  };

  /**
   * A song was recognized playing (XERK-184): open (or replace) the lyric box in
   * the cue box's slot — same place, same size — and let its lyrics auto-scroll
   * off the local clock. The scroll is client-driven: stamp the anchor now
   * (`anchorAt` = the wall-clock this client applied it, `anchorOffsetMs` = how
   * far into the track the server says we are), so `currentLyricIndex(song, now)`
   * reads straight off the real clock and the ticker carries the window forward
   * (renderSongBody). On the glasses lyrics beat translation beat cues (XERK-194):
   * the song takes the box over a live translation run (which is held, still
   * accumulating for the phone mirror) and over a cue (the api suppresses cues
   * while it plays) — a showing cue is embedded for review rather than dropped.
   * Only the interactive menu outranks the song, so behind it the song is held and
   * shows when the menu closes (closeMenu).
   */
  const showSong = (m: Song) => {
    state.song = {
      id: m.songId,
      title: m.title,
      artist: m.artist,
      lines: m.lines,
      anchorAt: Date.now(),
      anchorOffsetMs: m.offsetMs,
      durationMs: m.durationMs,
    };
    // A cue holding the box loses it to the song, embedded for review (XERK-108).
    if (state.cue) {
      embedPastCue(state.cue);
      state.cue = null;
      clearCueTimer();
    }
    // Only the interactive menu outranks the song box (XERK-194): behind it the
    // song is held and takes the box once the menu closes. A live translation run
    // yields the box to the song (it stays held, still accumulating), so opening
    // the song here repaints straight over it.
    if (!state.menu) rebuildPage();
    syncPhone();
  };

  /**
   * A fresh sync anchor for the current song (`song.sync`, XERK-184): re-anchor
   * its scroll to correct drift between the local clock and the true playback
   * position. Same song (matched by id), same lyrics — only (anchorAt, offset)
   * move. The repaint rides the ticker; nothing to do for a sync of some other
   * (stale) run.
   */
  const syncSong = (m: SongSync) => {
    if (!state.song || state.song.id !== m.songId) return;
    state.song.anchorAt = Date.now();
    state.song.anchorOffsetMs = m.offsetMs;
    // Re-anchoring can move the current line at once — repaint the visible box
    // now rather than waiting for the next tick.
    renderSongBody();
    sessionPage?.tickSong();
  };

  /** The current song ended (`song.done`, XERK-184): clear the box. */
  const finishSong = (m: SongDone) => {
    if (!state.song || state.song.id !== m.songId) return;
    dismissSong();
  };

  /**
   * The song box leaves (XERK-184): whatever the song outranked resumes now the
   * box is free (XERK-194). Precedence below it is translation > cue: a run that
   * was held behind the song retakes the box (or, if it finished while hidden, is
   * dismissed now with no on-lens countdown, XERK-181, draining a queued cue);
   * otherwise a queued cue pops, the same way `dismissTranslation` drains them.
   * The server held its own hold window before `song.done`, so the box is
   * dismissed at once. Nothing on screen changes while the menu owns the popup.
   */
  const dismissSong = () => {
    if (!state.song) return;
    state.song = null;
    if (state.menu) {
      syncPhone();
      return;
    }
    if (state.translation) {
      // A translation run was held behind the song. Finished while hidden → dismiss
      // it now (dismissTranslation drains a queued cue); still live → it retakes the
      // box and keeps stacking its turns.
      if (state.translation.done) dismissTranslation();
      else {
        rebuildPage();
        syncPhone();
      }
      return;
    }
    state.cue = state.cueQueue.shift() ?? null;
    if (state.cue) startCueTimer();
    rebuildPage();
    syncPhone();
  };

  /**
   * Show a private context cue (XERK-81): a bordered box above the transcript,
   * auto-dismissed after CUE_TTL_MS. Only one cue shows at a time (XERK-102): a
   * cue arriving while another is up — or while the interactive menu owns the
   * shared popup — is queued rather than clobbering what's there, and pops the
   * moment the box frees (dismissCue / closeMenu drain the queue).
   */
  const showCue = (cue: { id: string; title: string; body: string; source?: string }) => {
    // Anchor the cue to the last finalized turn the moment it arrives, so once
    // it's reviewed it lands inline right after the words that triggered it
    // (XERK-108). No turns yet → -1, i.e. it leads the transcript.
    const anchored: PastCue = { ...cue, afterIndex: state.segments.length - 1 };
    // Queue while the box is owned by the menu, another cue, a live translation
    // run, or a recognized song (XERK-160, XERK-184: cues don't appear during a
    // translation or a song — the api suppresses them, and this holds the line
    // against one already in flight when the run/song opened). The queue drains
    // when the box frees.
    if (state.menu || state.cue || state.translation || state.song) {
      state.cueQueue.push(anchored);
      // Bound the backlog: over the cap, drop the stalest waiting cue.
      if (state.cueQueue.length > MAX_QUEUED_CUES) state.cueQueue.shift();
      return;
    }
    state.cue = anchored;
    startCueTimer();
    rebuildPage();
    syncPhone();
  };

  /** A cue leaving the band drops into the phone transcript for review (XERK-108). */
  const embedPastCue = (cue: PastCue) => {
    state.pastCues.push(cue);
    // Bound the retained cues: over the cap, the oldest falls off.
    if (state.pastCues.length > MAX_PAST_CUES) state.pastCues.shift();
  };

  const dismissCue = () => {
    clearCueTimer();
    if (!state.cue) return;
    // The dismissed cue drops into the transcript for review (XERK-108), then
    // the next queued cue (if any) pops immediately (XERK-102); otherwise the box
    // frees back to the plain page. The menu never coexists with an active cue,
    // so there's no menu to guard against here.
    embedPastCue(state.cue);
    state.cue = state.cueQueue.shift() ?? null;
    if (state.cue) startCueTimer();
    rebuildPage();
    syncPhone();
  };

  /** Open the popup page: the bordered box on top, captions flowing around it. */
  const openMenu = () => {
    // The menu takes over the shared popup box — a cue showing there has had its
    // turn, so embed it in the transcript for review (XERK-108) before clearing.
    if (state.cue) {
      embedPastCue(state.cue);
      state.cue = null;
      clearCueTimer();
    }
    state.menu = "continue"; // Continue is the default
    rebuildPage();
  };

  /** Dismiss the popup: back to the plain page, captions full-band again. */
  const closeMenu = () => {
    state.menu = null;
    menuFallback = false;
    // Precedence below the menu is song > translation > cue (XERK-194): whatever
    // ranks highest among what's held retakes the freed box.
    if (state.song) {
      // A live song retakes the box when the menu closes (XERK-184), outranking a
      // held translation (which stays held) and a queued cue: rebuild to the song
      // box and leave the rest to drain when the song ends (dismissSong).
      rebuildPage();
      syncPhone();
      return;
    }
    if (state.translation) {
      // A translation run was held behind the menu (XERK-160). A run that
      // finished while the menu was up is dismissed now, as the box would
      // otherwise become visible: the run is over and there is no countdown to
      // wait out (XERK-181). A still-live run instead retakes the box and keeps
      // stacking its turns. dismissTranslation / rebuildPage each sync the phone.
      if (state.translation.done) dismissTranslation();
      else {
        rebuildPage();
        syncPhone();
      }
      return;
    }
    // A cue that arrived while the menu owned the popup now gets its turn (XERK-102).
    state.cue = state.cueQueue.shift() ?? null;
    if (state.cue) startCueTimer();
    rebuildPage();
    syncPhone();
  };

  /** Move the popup highlight (swipe): repaint the box — or the band, in fallback. */
  const moveMenuHighlight = (choice: MenuChoice) => {
    if (!state.menu || state.menu === choice) return;
    state.menu = choice;
    renderMenu();
    if (menuFallback) renderCaption();
  };

  /**
   * A touch on the active cue (XERK-129, XERK-133): ANY tap or swipe restarts
   * the auto-dismiss timer (and with it the countdown) — touching the cue means
   * it is being read, and reading buys it more time. The body scrolls under the
   * host's own native scroll (XERK-133), so the app moves nothing itself; it
   * just repaints the title row with the reset countdown, leaving the host's
   * scroll of the body alone.
   */
  const touchCue = () => {
    if (!state.cue) return;
    startCueTimer();
    renderMenu(); // repaint the title's reset countdown; the host owns the body scroll
  };

  function handleGesture(type: OsEventTypeList): void {
    switch (type) {
      case OsEventTypeList.CLICK_EVENT:
        if (!enabled) break;
        if (state.menu) {
          // In the popup a tap confirms the highlighted choice.
          if (state.menu === "exit") stopSession();
          else closeMenu();
        } else if (!state.recording) {
          // Idle: a single tap starts a new session.
          startSession();
        } else if (state.translation) {
          // A tap on the translation box does nothing (XERK-181): the box has no
          // countdown to reset, and the host owns the body scroll (XERK-133). It
          // dismisses itself when the run is done, not on a tap.
        } else if (state.song) {
          // A tap on the song box does nothing (XERK-184): the lyrics scroll off
          // the clock, there is no countdown, and the box clears on song.done.
        } else if (state.cue) {
          // A tap on a live cue keeps it up (XERK-129): restart the
          // auto-dismiss (and the countdown with it) so the wearer can hold
          // the cue open while reading. The tap does nothing else — XERK-85
          // still stands: it must never end the recording.
          touchCue();
        }
        // Recording with no menu and no cue: single taps do NOTHING (XERK-85
        // — a brushed temple must not end a recording).
        break;
      case OsEventTypeList.DOUBLE_CLICK_EVENT:
        if (enabled && state.recording) {
          // Pop up Continue (default) / Exit session; a second double tap
          // dismisses, same as Continue.
          if (state.menu) closeMenu();
          else openMenu();
          break;
        }
        // Outside a session: canonical app exit — confirm dialog; real
        // teardown happens on SYSTEM_EXIT.
        void bridge.shutDownPageContainer(1);
        break;
      case OsEventTypeList.SCROLL_TOP_EVENT:
        // Swipe up: highlight the menu's top row (Continue). On a cue the app
        // resets the countdown while the host scrolls the body toward its start
        // (XERK-133); on a translation box the host scrolls the body and the app
        // does nothing else — there is no countdown (XERK-181). A song box has no
        // countdown either and its lyrics scroll on their own (XERK-184), so it is
        // left alone too. Anywhere else the gesture lands on the invisible overlay.
        if (state.menu) moveMenuHighlight("continue");
        else if (!state.translation && !state.song) touchCue();
        break;
      case OsEventTypeList.SCROLL_BOTTOM_EVENT:
        // Swipe down: highlight the menu's bottom row (Exit session). On a cue
        // the app resets the countdown while the host scrolls the body toward its
        // end (XERK-133); on a translation box the host scrolls and the app does
        // nothing else — no countdown to reset (XERK-181). A song box scrolls its
        // lyrics on its own and has no countdown (XERK-184), so it is left alone.
        if (state.menu) moveMenuHighlight("exit");
        else if (!state.translation && !state.song) touchCue();
        break;
      case OsEventTypeList.FOREGROUND_ENTER_EVENT:
        // Back in the foreground (XERK-117): a snapshot taken from here on is a
        // foreground one, so re-persist the running session as non-resumable —
        // if the app is now killed it must not reopen this session on next boot.
        backgrounded = false;
        if (state.recording) persist();
        // The host may have redrawn while we were away: drop the writer's
        // dedupe cache and repaint everything (the popup box included, if it is
        // up) so a stale host frame can't linger. The ticker keeps the clock,
        // dots and cue count live even while backgrounded (XERK-113); this just
        // forces a clean, full resync on return.
        writer.invalidate();
        if (!enabled) showSignInPrompt();
        else if (state.recording) {
          renderStatus();
          renderClock();
          renderCaption();
          renderMenu();
          renderSongBody(); // a live song box: repaint its scrolling lyrics too (XERK-184)
        } else {
          showIdle();
        }
        break;
      case OsEventTypeList.FOREGROUND_EXIT_EVENT:
        // Backgrounded, not closed (XERK-117): the app stays alive over BLE and
        // may be migrated to a headless context, so mark the running session
        // resumable and flush it now — the next boot restores it. A close fires
        // SYSTEM_EXIT/ABNORMAL_EXIT instead, handled below. Keep rendering: the
        // glasses still show the lens over BLE while backgrounded (XERK-113).
        backgrounded = true;
        if (state.recording) persist();
        void store.flush();
        break;
      case OsEventTypeList.SYSTEM_EXIT_EVENT:
      case OsEventTypeList.ABNORMAL_EXIT_EVENT:
        void cleanup();
        break;
    }
  }

  return { enable, disable };
}
