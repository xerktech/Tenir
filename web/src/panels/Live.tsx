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
  DISCLOSURES,
  isPinnedToBottom,
  liveTranscript,
} from "@tenir/client-core";
import { useEffect, useRef, useState } from "react";

import { useCaptureContext } from "../lib/capture";
import { acceptRecordingNotice, recordingNoticeAccepted } from "../lib/consent";
import { type CaptureController } from "../lib/useCapture";
import { Badge, Button, Card, CueDisclosure, EmptyState } from "../ui";

const RECORDING_NOTICE = DISCLOSURES.find((d) => d.id === "recording");

type ActiveCue = CaptureController["state"]["activeCue"];

/**
 * Keep a released cue mounted for the length of its fade-out (XERK-107).
 *
 * Returns the cue to paint plus whether it is on its way out, so the band can
 * transition to nothing instead of blinking off the screen. A cue that arrives
 * mid-fade cancels the pending unmount and takes over immediately, matching the
 * "only one cue at a time" rule (XERK-102).
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
 * The countdown is derived from a timestamp taken when the cue appeared rather
 * than decremented per tick, so a throttled tab (or a slow frame) resyncs to
 * the truth instead of accumulating drift away from the release timer in
 * `CaptureSession`. It restarts whenever a different cue takes the band —
 * including a queued cue promoted into it, which gets its own full countdown.
 */
function useCueCountdown(cueId: string | undefined): number {
  const [secondsLeft, setSecondsLeft] = useState(() => cueSecondsLeft(0));
  useEffect(() => {
    if (!cueId) return;
    setSecondsLeft(cueSecondsLeft(0));
    const startedAt = Date.now();
    const timer = window.setInterval(
      () => setSecondsLeft(cueSecondsLeft(Date.now() - startedAt)),
      CUE_COUNTDOWN_TICK_MS,
    );
    return () => window.clearInterval(timer);
  }, [cueId]);
  return secondsLeft;
}

/**
 * The single active private-context cue over the live transcript (XERK-102).
 * One cue shows at a time; any others wait in a FIFO queue and pop the moment
 * this one is released. When the queue is non-empty a small "+N more" note tells
 * the wearer more cues are lined up.
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
  queuedCount,
}: {
  activeCue: ActiveCue;
  queuedCount: number;
}): JSX.Element | null {
  const { cue, exiting } = useCueExit(activeCue);
  const secondsLeft = useCueCountdown(cue?.id);
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
                  <li key={item.segment.id}>{item.segment.text}</li>
                ) : (
                  // A released cue, embedded inline as a collapsed dropdown (XERK-108).
                  <li className="transcript-cue" key={`cue-${item.cue.id}`}>
                    <CueDisclosure title={item.cue.title} body={item.cue.body} />
                  </li>
                ),
              )}
              {state.partial && <li className="muted">{state.partial}</li>}
            </ul>
          </div>
        )}
        <LiveCueBand activeCue={state.activeCue} queuedCount={state.queuedCues.length} />
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
