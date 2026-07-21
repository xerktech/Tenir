/**
 * Live recording panel — the web counterpart to the mobile `Live` screen.
 *
 * `LivePanel` wires the shared `CaptureSession` (via `useCapture`) to the configured
 * server's WS URL and gates the first capture behind the recording notice. `LiveView`
 * is the presentational surface (driven by a `CaptureController`) so it renders under
 * test without a real mic or socket.
 */

import { DISCLOSURES, wsFromHttpBase } from "@tenir/client-core";
import { useState } from "react";

import { getServerUrl } from "../config";
import { acceptRecordingNotice, recordingNoticeAccepted } from "../lib/consent";
import { useCapture, type CaptureController } from "../lib/useCapture";
import { Badge, Button, Card, EmptyState } from "../ui";

const RECORDING_NOTICE = DISCLOSURES.find((d) => d.id === "recording");

export function LiveView({ controller }: { controller: CaptureController }): JSX.Element {
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
      </div>

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
  const controller = useCapture(wsFromHttpBase(getServerUrl()));

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
