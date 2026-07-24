/**
 * Live recording panel — the web counterpart to the mobile `Live` screen.
 *
 * `LivePanel` wires the shared `CaptureSession` (via `useCapture`) to the configured
 * server's WS URL and gates the first capture behind the recording notice. `LiveView`
 * is the presentational surface (driven by a `CaptureController`) so it renders under
 * test without a real mic or socket.
 */

import {
  CUE_EXIT_MS,
  DISCLOSURES,
  isPinnedToBottom,
  wsFromHttpBase,
  type CueLevel,
} from "@tenir/client-core";
import { useEffect, useRef, useState } from "react";

import { getServerUrl } from "../config";
import { acceptRecordingNotice, recordingNoticeAccepted } from "../lib/consent";
import { CUE_LEVELS, loadCueLevel, saveCueLevel } from "../lib/cueLevelStore";
import { useCapture, type CaptureController } from "../lib/useCapture";
import { Badge, Button, Card, EmptyState } from "../ui";

const RECORDING_NOTICE = DISCLOSURES.find((d) => d.id === "recording");

const CUE_LEVEL_LABEL: Record<CueLevel, string> = {
  conservative: "Conservative",
  balanced: "Balanced",
  aggressive: "Aggressive",
};

/** Global toggle for how eagerly private context cues appear (XERK-81). */
export function CueLevelToggle({
  level,
  onChange,
}: {
  level: CueLevel;
  onChange: (l: CueLevel) => void;
}): JSX.Element {
  return (
    <div className="cue-level" role="group" aria-label="Cue detail level">
      <span className="cue-level-caption muted">Cues</span>
      {CUE_LEVELS.map((l) => (
        <button
          key={l}
          type="button"
          className={`cue-level-option ${l === level ? "active" : ""}`.trim()}
          aria-pressed={l === level}
          onClick={() => onChange(l)}
        >
          {CUE_LEVEL_LABEL[l]}
        </button>
      ))}
    </div>
  );
}

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
  if (!cue) return null;
  return (
    <div
      className={`cue-band ${exiting ? "exiting" : ""}`.trim()}
      aria-live="polite"
      aria-hidden={exiting || undefined}
    >
      <div className="cue-card" key={cue.id}>
        <div className="cue-card-title">{cue.title}</div>
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

export function LiveView({
  controller,
  cueLevel,
  onCueLevelChange,
}: {
  controller: CaptureController;
  cueLevel: CueLevel;
  onCueLevelChange: (l: CueLevel) => void;
}): JSX.Element {
  const { state } = controller;
  const hasContent = state.segments.length > 0 || Boolean(state.partial);

  // The transcript scrolls inside its own bounded box so a long session never
  // scrolls the page and carries the cue band out of view (XERK-103). Keep the
  // box following the newest caption while the viewer is at the bottom; once
  // they scroll up to re-read, stop yanking them back down (shared geometry).
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [state.segments.length, state.partial]);
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
        <span className="grow" />
        <CueLevelToggle level={cueLevel} onChange={onCueLevelChange} />
      </div>

      {/* The cue floats inside this card, over the transcript's top edge, so it
          never displaces the captions as it comes and goes (XERK-107). */}
      <Card className="cue-stage">
        {!hasContent ? (
          <EmptyState title="No captions yet." hint="Press Record to start a live conversation." />
        ) : (
          <div className="transcript-scroll" ref={scrollRef} onScroll={onTranscriptScroll}>
            <ul className="transcript">
              {state.segments.map((s) => (
                <li key={s.id}>{s.text}</li>
              ))}
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
  const [cueLevel, setCueLevel] = useState<CueLevel>(() => loadCueLevel());
  const controller = useCapture(wsFromHttpBase(getServerUrl()), cueLevel);

  const changeCueLevel = (l: CueLevel) => {
    setCueLevel(l);
    saveCueLevel(l);
  };

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

  return <LiveView controller={controller} cueLevel={cueLevel} onCueLevelChange={changeCueLevel} />;
}
