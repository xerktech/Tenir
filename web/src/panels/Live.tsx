/**
 * Live recording panel — the web counterpart to the mobile `Live` screen.
 *
 * `LivePanel` wires the shared `CaptureSession` (via `useCapture`) to the configured
 * server's WS URL and gates the first capture behind the recording notice. `LiveView`
 * is the presentational surface (driven by a `CaptureController`) so it renders under
 * test without a real mic or socket.
 */

import { DISCLOSURES, wsFromHttpBase, type CueLevel } from "@tenir/client-core";
import { useState } from "react";

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

/**
 * The single active private-context cue above the live transcript (XERK-102).
 * One cue shows at a time; any others wait in a FIFO queue and pop the moment
 * this one is released. When the queue is non-empty a small "+N more" note tells
 * the wearer more cues are lined up.
 */
function LiveCueBand({
  activeCue,
  queuedCount,
}: {
  activeCue: CaptureController["state"]["activeCue"];
  queuedCount: number;
}): JSX.Element | null {
  if (!activeCue) return null;
  return (
    <div className="cue-band" aria-live="polite">
      <div className="cue-card" key={activeCue.id}>
        <div className="cue-card-title">{activeCue.title}</div>
        <div className="cue-card-body">{activeCue.body}</div>
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
  return (
    <section>
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

      <LiveCueBand activeCue={state.activeCue} queuedCount={state.queuedCues.length} />

      <Card>
        {state.segments.length === 0 && !state.partial ? (
          <EmptyState title="No captions yet." hint="Press Record to start a live conversation." />
        ) : (
          <ul className="transcript">
            {state.segments.map((s) => (
              <li key={s.id}>{s.text}</li>
            ))}
            {state.partial && <li className="muted">{state.partial}</li>}
          </ul>
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
