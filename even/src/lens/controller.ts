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

import { ApiClient, cueSecondsLeft, type ApiHandlers, type SessionParams } from "@tenir/client-core";

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
  cueRowRange,
  cueTitleLine,
  fitCaption,
  menuText,
  occludedCaption,
  statusLine,
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
// non-English run keeps appending turn translations; the box shows a 4-row
// window the host scrolls, so only the most recent text needs to be kept.
export const TRANSLATION_MAX_CHARS = 1200;
// The translation box's pinned title row (XERK-160). The box reuses the cue
// box's geometry — same place, same size — so the wearer reads one popup shape.
export const TRANSLATION_TITLE = "Translation";
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
  // turns currently being spoken, shown in the cue box's slot. `done` flips when
  // the api says the other language is done being spoken (`translation.done`) —
  // only then does the box's 10s auto-dismiss countdown start.
  translation: { texts: string[]; done: boolean } | null;
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
  };
  let cueTimer: ReturnType<typeof setTimeout> | null = null;
  // When the cue currently in the box went up, so its countdown (XERK-110) can
  // be derived on every tick rather than decremented — the ticker idles while
  // the lens is backgrounded, and a derived count comes back correct.
  let cueShownAt: number | null = null;
  // The translation box's auto-dismiss (XERK-160): armed only once the run is
  // done — the countdown must not start while the other language is still being
  // spoken. `translationDoneAt` derives its countdown, like `cueShownAt` above.
  let translationTimer: ReturnType<typeof setTimeout> | null = null;
  let translationDoneAt: number | null = null;
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
  // translations stacked as its body.
  const translationCard = (): CueCard => ({
    title: TRANSLATION_TITLE,
    body: (state.translation?.texts ?? []).join("\n"),
  });
  // Seconds left before the translation box auto-dismisses — undefined while the
  // other language is still being spoken (the countdown hasn't started, XERK-160).
  const translationCountdown = () =>
    translationDoneAt == null ? undefined : cueSecondsLeft(Date.now() - translationDoneAt);
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
    // (XERK-119) — a short box frees the transcript rows below it. The menu
    // outranks a held translation, so its range wins while it is open.
    const [first, last] = state.menu
      ? [MENU_ROW_FIRST, MENU_ROW_LAST]
      : state.translation
        ? cueRowRange(translationCard())
        : state.cue
          ? cueRowRange(state.cue)
          : [MENU_ROW_FIRST, MENU_ROW_LAST];
    return occludedCaption(bounded, first, last);
  };
  // A popup strip is up: the interactive menu, a live translation box (XERK-160),
  // or a private-context cue box (XERK-81). All live in the same bordered
  // container across the top.
  const popupUp = () => state.menu !== null || state.translation !== null || state.cue !== null;
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
    if (state.menu) writer.set(CONTAINER.menu, menuText(state.menu));
    else if (state.translation)
      writer.set(CONTAINER.menu, cueTitleLine(translationCard(), translationCountdown()));
    else if (state.cue) writer.set(CONTAINER.menu, cueTitleLine(state.cue, cueCountdown()));
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
    });

  /**
   * Swap the page between the plain layout and the one with the popup box —
   * `rebuildPageContainer` is the SDK's sanctioned runtime page change. Rides
   * the writer's serialized lane, then re-asserts every container's text so a
   * stale queued write from just before the swap can't land on the new page.
   */
  const rebuildPage = () => {
    const contents = pageContents();
    // The menu outranks everything (it is interactive); a live translation run
    // outranks a cue (XERK-160: cues neither trigger nor appear mid-run). The
    // translation box IS the cue box — same page shape, same geometry.
    const page = state.menu
      ? buildMenuPage(contents, state.menu)
      : state.translation
        ? buildCuePage(contents, translationCard(), translationCountdown())
        : state.cue
          ? buildCuePage(contents, state.cue, cueCountdown())
          : buildMainPage(contents);
    const openingMenu = state.menu !== null;
    const openingTranslation = state.menu === null && state.translation !== null;
    const openingCue = state.menu === null && state.translation === null && state.cue !== null;
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
        clearTranslationTimer();
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
      onTranslation: (m) => showTranslation(m.segmentId, m.text),
      onTranslationDone: () => finishTranslation(),
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
    clearTranslationTimer();
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
    clearCueTimer();
    clearTranslationTimer();
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
    if (state.cue) {
      renderMenu();
      sessionPage?.tickCue(cueCountdown());
    } else if (state.translation?.done) {
      // A finished run is counting down to dismissal (XERK-160): keep the
      // number in the box's title row current, like a cue's.
      renderMenu();
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
    if (translationTimer) clearTimeout(translationTimer);
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
   * Arm (or re-arm) the translation box's auto-dismiss (XERK-160). Only ever
   * called once the run is done — the 10s countdown must not start while the
   * other language is still being spoken — and again on a touch (XERK-129
   * parity: touching the box means it is being read, and reading buys time).
   */
  const startTranslationTimer = () => {
    if (translationTimer) clearTimeout(translationTimer);
    translationDoneAt = Date.now();
    translationTimer = setTimeout(() => dismissTranslation(), CUE_TTL_MS);
  };

  const clearTranslationTimer = () => {
    if (translationTimer) clearTimeout(translationTimer);
    translationTimer = null;
    translationDoneAt = null;
  };

  /**
   * An English translation of one finalized non-English turn arrived (XERK-160).
   * Two jobs: pair it with its turn for the phone mirror, and put it in the
   * on-lens box — the cue box's slot, place and size (the ticket's contract).
   * A run in progress appends turn after turn (the host scrolls the body, like
   * a long cue); a fresh run replaces a finished box still counting down. A cue
   * holding the box loses it — cues don't appear during translations — and is
   * embedded for review rather than dropped.
   */
  const showTranslation = (segmentId: string, text: string) => {
    const seg = state.segments.find((s) => s.id === segmentId);
    if (seg) seg.translation = text;
    if (!state.translation || state.translation.done) {
      state.translation = { texts: [text], done: false };
      clearTranslationTimer();
      if (state.cue) {
        embedPastCue(state.cue);
        state.cue = null;
        clearCueTimer();
      }
    } else {
      state.translation.texts.push(text);
      // Bound the accumulated body like the transcript: the box shows a
      // scrolling window, so only the most recent text needs keeping.
      while (
        state.translation.texts.length > 1 &&
        state.translation.texts.join("\n").length > TRANSLATION_MAX_CHARS
      ) {
        state.translation.texts.shift();
      }
    }
    // The menu outranks the box (it is interactive): a run behind it keeps
    // accumulating and shows when the menu closes.
    if (!state.menu) rebuildPage();
    syncPhone();
  };

  /**
   * The api says the other language is done being spoken (`translation.done`,
   * XERK-160): NOW the box's 10s countdown starts. Behind the open menu it
   * waits — `closeMenu` arms it when the box actually becomes visible.
   */
  const finishTranslation = () => {
    if (!state.translation || state.translation.done) return;
    state.translation.done = true;
    if (!state.menu) {
      startTranslationTimer();
      renderMenu(); // paint the countdown into the pinned title row
    }
  };

  /** The translation box leaves; queued cues resume now the run is over (XERK-160). */
  const dismissTranslation = () => {
    clearTranslationTimer();
    if (!state.translation) return;
    state.translation = null;
    if (!state.menu) {
      state.cue = state.cueQueue.shift() ?? null;
      if (state.cue) startCueTimer();
      rebuildPage();
    }
    syncPhone();
  };

  /**
   * A touch on the translation box (XERK-129 parity): once the run is done and
   * counting down, any tap or swipe restarts the countdown — touching the box
   * means it is being read. While the run is still live there is no countdown
   * to reset, and the host owns the body scroll either way (XERK-133).
   */
  const touchTranslation = () => {
    if (!state.translation?.done) return;
    startTranslationTimer();
    renderMenu();
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
    // Queue while the box is owned by the menu, another cue, or a live
    // translation run (XERK-160: cues don't appear during translations — the
    // api suppresses them, and this holds the line against one already in
    // flight when the run opened). The queue drains when the box frees.
    if (state.menu || state.cue || state.translation) {
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
    if (state.translation) {
      // A translation run held behind the menu takes the box back (XERK-160).
      // A run that finished while the menu was open starts its countdown only
      // now, as the box actually becomes visible.
      if (state.translation.done && translationTimer == null) startTranslationTimer();
    } else {
      // A cue that arrived while the menu owned the popup now gets its turn (XERK-102).
      state.cue = state.cueQueue.shift() ?? null;
      if (state.cue) startCueTimer();
    }
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
          // A tap on the translation box keeps it up once its countdown runs
          // (XERK-129 parity); mid-run it does nothing — there is no countdown.
          touchTranslation();
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
        // Swipe up: highlight the menu's top row (Continue). On a cue or a
        // translation box the host itself scrolls the body toward its start
        // (XERK-133); the app just resets the countdown. Anywhere else the
        // gesture lands on the invisible overlay and does nothing.
        if (state.menu) moveMenuHighlight("continue");
        else if (state.translation) touchTranslation();
        else touchCue();
        break;
      case OsEventTypeList.SCROLL_BOTTOM_EVENT:
        // Swipe down: highlight the menu's bottom row (Exit session). On a cue
        // or a translation box the host scrolls the body toward its end
        // (XERK-133); the app just resets the countdown.
        if (state.menu) moveMenuHighlight("exit");
        else if (state.translation) touchTranslation();
        else touchCue();
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
