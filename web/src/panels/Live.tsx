/**
 * Live recording panel — the web counterpart to the mobile `Live` screen.
 *
 * `LivePanel` wires the shared `CaptureSession` (via `useCapture`) to the configured
 * server's WS URL and gates the first capture behind the recording notice. `LiveView`
 * is the presentational surface (driven by a `CaptureController`) so it renders under
 * test without a real mic or socket.
 */

import {
  CUE_COUNTDOWN_TICK_MS,
  CUE_EXIT_MS,
  cueCountdownLabel,
  cueSecondsLeft,
  cueSecondsUntil,
  currentLyricIndex,
  DISCLOSURES,
  isPinnedToBottom,
  type LiveSong,
  liveTranscript,
  type LyricWindow,
  lyricWindow,
} from "@tenir/client-core";
import { useEffect, useRef, useState } from "react";

import { useCaptureContext } from "../lib/capture";
import { acceptRecordingNotice, recordingNoticeAccepted } from "../lib/consent";
import { type CaptureController } from "../lib/useCapture";
import { Badge, Button, Card, CueDisclosure, EmptyState, LangTag, TranslationLine } from "../ui";

const RECORDING_NOTICE = DISCLOSURES.find((d) => d.id === "recording");

type ActiveCue = CaptureController["state"]["activeCue"];

/**
 * Keep a released cue mounted for the length of its fade-out (XERK-107).
 *
 * Returns the cue to paint plus whether it is on its way out, so the band can
 * transition to nothing instead of blinking off the screen. A cue that arrives
 * mid-fade cancels the pending unmount and takes over immediately, matching the
 * "one cue at a time, freshest wins" rule (XERK-102, XERK-159).
 */
function useCueExit(activeCue: ActiveCue): { cue: ActiveCue; exiting: boolean } {
  const [painted, setPainted] = useState<ActiveCue>(activeCue);
  useEffect(() => {
    if (activeCue) {
      setPainted(activeCue);
      return;
    }
    const timer = window.setTimeout(() => setPainted(null), CUE_EXIT_MS);
    return () => window.clearTimeout(timer);
  }, [activeCue]);
  return { cue: painted, exiting: !activeCue && painted != null };
}

/**
 * Count the seconds a cue has left on screen (XERK-110).
 *
 * Derived from the cue's wall-clock end time (`activeCueEndsAt`, XERK-159) read
 * against the real clock rather than decremented per tick, so a throttled tab —
 * or the whole app coming back from the background — resyncs to the truth instead
 * of drifting from the release timer in `CaptureSession`. A cue whose turn opened
 * while the app was away therefore shows the time it actually has left, not a
 * fresh ten. It restarts whenever a different cue takes the band, and holds its
 * last value through the exit fade (when `endsAt` is null).
 */
function useCueCountdown(endsAt: number | null): number {
  const [secondsLeft, setSecondsLeft] = useState(() => cueSecondsLeft(0));
  useEffect(() => {
    if (endsAt == null) return;
    const tick = () => setSecondsLeft(cueSecondsUntil(endsAt, Date.now()));
    tick();
    const timer = window.setInterval(tick, CUE_COUNTDOWN_TICK_MS);
    return () => window.clearInterval(timer);
  }, [endsAt]);
  return secondsLeft;
}

/**
 * The single active private-context cue over the live transcript (XERK-102).
 * One cue shows at a time for its full turn; any others wait in a FIFO queue and
 * take the band as each turn ends. The turns run on a continuous wall-clock
 * schedule that keeps advancing while the app is backgrounded (XERK-159), so a
 * cue whose turn passed while you were away is already in the transcript on
 * return rather than replaying — you rejoin the live cue mid-turn, not a backlog.
 * When the queue is non-empty a small "+N more" note says more cues are lined up.
 *
 * The band *floats over* the top of the transcript box rather than sitting above
 * it in the flow (XERK-107): a cue arriving or expiring used to push the
 * transcript down and let it snap back, which is disorienting mid-conversation.
 * As an overlay it changes no layout at all — it briefly covers the oldest
 * visible captions instead, which are the ones nobody is reading, since the box
 * follows the newest text at the bottom. It is click-through except for the card
 * itself, so the transcript underneath still scrolls and selects.
 */
function LiveCueBand({
  activeCue,
  activeCueEndsAt,
  queuedCount,
}: {
  activeCue: ActiveCue;
  activeCueEndsAt: number | null;
  queuedCount: number;
}): JSX.Element | null {
  const { cue, exiting } = useCueExit(activeCue);
  const secondsLeft = useCueCountdown(activeCueEndsAt);
  if (!cue) return null;
  return (
    <div
      className={`cue-band ${exiting ? "exiting" : ""}`.trim()}
      aria-live="polite"
      aria-hidden={exiting || undefined}
    >
      <div className="cue-card" key={cue.id}>
        <div className="cue-card-head">
          <div className="cue-card-title">{cue.title}</div>
          {/* The countdown to auto-dismissal (XERK-110), across from the title.
              Hidden from the accessibility tree on purpose: the card announces
              itself politely, and a number changing every second inside that
              live region would re-announce the whole cue once a second. */}
          <div className="cue-card-countdown" aria-hidden="true">
            {cueCountdownLabel(secondsLeft)}
          </div>
        </div>
        <div className="cue-card-body">{cue.body}</div>
        {/* Where the fact came from (XERK-120): the live source it was grounded
            in. Absent for a cue from the model's own knowledge. */}
        {cue.source && <div className="cue-card-source">{cue.source}</div>}
      </div>
      {queuedCount > 0 && (
        <div
          className="cue-queued muted"
          aria-label={`${queuedCount} more ${queuedCount === 1 ? "cue" : "cues"} queued`}
        >
          +{queuedCount} more
        </div>
      )}
    </div>
  );
}

// How often the lyric scroll recomputes which line is current (XERK-184).
// Comfortably sub-second so the highlighted line turns over within a frame or
// two of the beat, matching the cue countdown's cadence.
const LYRIC_SCROLL_TICK_MS = 250;

/**
 * Advance the visible lyric window off the local clock (XERK-184). Recomputes the
 * current line from the song's scroll anchor every tick, so the window scrolls
 * smoothly between the server's periodic `song.sync` re-anchors — and a throttled
 * or backgrounded tab resyncs to the truth on the next tick rather than drifting.
 * Re-seeds whenever the song object changes (a new song, or a re-anchored one).
 */
function useLyricScroll(song: LiveSong | null): LyricWindow {
  const [win, setWin] = useState<LyricWindow>({ lines: [], currentIndex: -1 });
  useEffect(() => {
    if (!song) {
      setWin({ lines: [], currentIndex: -1 });
      return;
    }
    const tick = () => setWin(lyricWindow(song.lines, currentLyricIndex(song, Date.now())));
    tick();
    const timer = window.setInterval(tick, LYRIC_SCROLL_TICK_MS);
    return () => window.clearInterval(timer);
  }, [song]);
  return win;
}

/**
 * The recognized song's synced lyrics, auto-scrolling in the same box a cue uses
 * (XERK-184). Title is "TITLE — ARTIST"; the body is a fixed window of lyric
 * lines with the current one highlighted, advancing as the song plays. A live
 * song owns the box — the cue band is hidden while it shows. Floats over the
 * transcript exactly like the cue band, so nothing reflows.
 */
function LiveLyricsBand({ song }: { song: LiveSong | null }): JSX.Element | null {
  const win = useLyricScroll(song);
  if (!song) return null;
  return (
    <div className="cue-band lyrics-band" aria-hidden="true">
      <div className="cue-card lyrics-card" key={song.id}>
        <div className="cue-card-head">
          <div className="cue-card-title">
            {song.title} — {song.artist}
          </div>
          <div className="cue-card-badge">♪</div>
        </div>
        <div className="lyrics-body">
          {win.lines.length === 0 ? (
            // No synced lyrics were found: show the title, no scroll.
            <div className="lyrics-line lyrics-empty muted">♪ ♪ ♪</div>
          ) : (
            win.lines.map((ln, i) => (
              <div
                key={`${ln.atMs}-${i}`}
                className={`lyrics-line ${i === win.currentIndex ? "current" : ""}`.trim()}
              >
                {ln.text || "♪"}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

export function LiveView({ controller }: { controller: CaptureController }): JSX.Element {
  const { state } = controller;
  const hasContent = state.segments.length > 0 || state.pastCues.length > 0 || Boolean(state.partial);

  // Interleave finalized turns with the cues already released from the band, so a
  // past cue can be re-read inline without disturbing the cues still coming in
  // above (XERK-108) — the same segment/cue timeline the history detail renders.
  const items = liveTranscript(state.segments, state.pastCues);

  // The transcript scrolls inside its own bounded box so a long session never
  // scrolls the page and carries the cue band out of view (XERK-103). Keep the
  // box following the newest caption while the viewer is at the bottom; once
  // they scroll up to re-read, stop yanking them back down (shared geometry).
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [state.segments.length, state.pastCues.length, state.partial]);
  const onTranscriptScroll = () => {
    const el = scrollRef.current;
    if (el) pinnedRef.current = isPinnedToBottom(el);
  };

  return (
    <section className="live">
      <div className="row">
        <h2 className="grow">Live</h2>
        <Badge tone={state.connection === "open" ? "accent" : "neutral"}>{state.connection}</Badge>
      </div>

      {state.error && <p className="muted">{state.error}</p>}

      <div className="row">
        {state.running ? (
          <>
            <Button variant="danger" onClick={() => void controller.stop()}>
              Stop
            </Button>
            <Button onClick={() => controller.togglePause()}>
              {state.listening ? "Pause" : "Resume"}
            </Button>
          </>
        ) : (
          <Button variant="primary" onClick={() => void controller.start()}>
            Record
          </Button>
        )}
      </div>

      {/* The cue floats inside this card, over the transcript's top edge, so it
          never displaces the captions as it comes and goes (XERK-107). */}
      <Card className="cue-stage">
        {!hasContent ? (
          <EmptyState title="No captions yet." hint="Press Record to start a live conversation." />
        ) : (
          <div className="transcript-scroll" ref={scrollRef} onScroll={onTranscriptScroll}>
            <ul className="transcript">
              {items.map((item) =>
                item.kind === "segment" ? (
                  <li key={item.segment.id}>
                    {/* Source-language chip on a translated turn's original text
                        (XERK-160) — the counterpart of the translation's "EN" tag. */}
                    {item.segment.translation && item.segment.lang && (
                      <LangTag lang={item.segment.lang} />
                    )}
                    {item.segment.text}
                    {/* English translation of a non-English turn (XERK-160),
                        turn-by-turn under the original as it arrives. */}
                    {item.segment.translation && (
                      <TranslationLine text={item.segment.translation} lang={item.segment.lang} />
                    )}
                  </li>
                ) : (
                  // A released cue, embedded inline as a collapsed dropdown (XERK-108).
                  <li className="transcript-cue" key={`cue-${item.cue.id}`}>
                    <CueDisclosure
                      title={item.cue.title}
                      body={item.cue.body}
                      source={item.cue.source}
                    />
                  </li>
                ),
              )}
              {state.partial && <li className="muted">{state.partial}</li>}
            </ul>
          </div>
        )}
        {/* A recognized song's lyrics own the box (XERK-184); the cue band is
            hidden while it plays, exactly as cues stand aside for it server-side. */}
        {state.song ? (
          <LiveLyricsBand song={state.song} />
        ) : (
          <LiveCueBand
            activeCue={state.activeCue}
            activeCueEndsAt={state.activeCueEndsAt}
            queuedCount={state.queuedCues.length}
          />
        )}
      </Card>
    </section>
  );
}

function RecordingNotice({ onAccept }: { onAccept: () => void }): JSX.Element {
  return (
    <section>
      <h2>{RECORDING_NOTICE?.title ?? "Recording notice"}</h2>
      <p className="muted">{RECORDING_NOTICE?.body}</p>
      <Button variant="primary" onClick={onAccept}>
        I understand
      </Button>
    </section>
  );
}

export function LivePanel(): JSX.Element {
  const [accepted, setAccepted] = useState(() => recordingNoticeAccepted());
  // The session lives in the app-level capture context so a live recording
  // survives switching to another tab and back (XERK-111).
  const { controller } = useCaptureContext();

  if (!accepted) {
    return (
      <RecordingNotice
        onAccept={() => {
          acceptRecordingNotice();
          setAccepted(true);
        }}
      />
    );
  }

  return <LiveView controller={controller} />;
}
