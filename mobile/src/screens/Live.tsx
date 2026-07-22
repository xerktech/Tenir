/**
 * Live capture: phone-mic transcription rendered on the phone screen.
 *
 * Tap Start to stream the phone mic to the api and watch captions land live.
 * Pause stops uploading without dropping the session; Stop ends it.
 *
 * A thin presenter over `useCapture`: all the session behaviour lives in the tested,
 * framework-agnostic `CaptureSession`.
 */

import { useState } from "react";
import { Text, View } from "react-native";

import { DISCLOSURES } from "@tenir/client-core";
import { useCapture } from "../lib/useCapture";
import { useNotify } from "../lib/notify";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Heading,
  ListItem,
  Muted,
  Row,
  Screen,
} from "../ui/components";
import { useTheme } from "../ui/ThemeContext";

const RECORDING_NOTICE = DISCLOSURES.find((d) => d.id === "recording")?.body ?? "";

function connectionLabel(state: ReturnType<typeof useCapture>["state"]): string {
  if (!state.running) return "idle";
  if (state.connection === "open") return state.listening ? "● live" : "❚❚ paused";
  if (state.connection === "connecting") return "… connecting";
  return "× reconnecting";
}

export function LiveScreen({ wsUrl }: { wsUrl: string }): JSX.Element {
  const cap = useCapture(wsUrl);
  const notify = useNotify();
  const { colors } = useTheme();
  const [busy, setBusy] = useState(false);
  const { state } = cap;

  const start = async () => {
    setBusy(true);
    const ok = await cap.start();
    setBusy(false);
    if (!ok) notify("Microphone unavailable — check the app's mic permission.", "err");
  };
  const stop = async () => {
    setBusy(true);
    await cap.stop();
    setBusy(false);
  };

  const mic = state.micSource === "g2-microphone" ? "glasses mic" : "phone mic";
  const live = state.running && state.connection === "open" && state.listening;

  return (
    <Screen>
      <Heading>Live</Heading>

      <Card>
        <Row>
          {/* Connection state as a tinted pill, matching the web Live badge. */}
          <Badge tone={live ? "accent" : "neutral"}>{connectionLabel(state)}</Badge>
          <View style={{ flexGrow: 1 }} />
          <Muted>{mic}</Muted>
        </Row>
        <Row>
          {!state.running ? (
            <Button title="Start" kind="primary" onPress={start} disabled={busy} />
          ) : (
            <>
              <Button
                title={state.listening ? "Pause" : "Resume"}
                onPress={cap.togglePause}
                disabled={busy}
              />
              <Button
                title={state.micSource === "g2-microphone" ? "Use phone mic" : "Use glasses mic"}
                onPress={() =>
                  cap.switchMic(
                    state.micSource === "g2-microphone" ? "phone-microphone" : "g2-microphone",
                  )
                }
                disabled={busy}
              />
              <Button title="Stop" kind="danger" onPress={stop} disabled={busy} />
            </>
          )}
        </Row>
        {!state.running && <Muted>{RECORDING_NOTICE}</Muted>}
      </Card>

      {state.segments.length === 0 && !state.partial && !state.running ? (
        <EmptyState title="No captions yet." hint="Press Start to begin a live conversation." />
      ) : null}
      {state.segments.length === 0 && !state.partial && state.running ? (
        <Muted>Listening…</Muted>
      ) : null}

      {state.segments.map((seg) => (
        <ListItem key={seg.id}>
          <Text style={{ color: colors.text }}>{seg.text}</Text>
        </ListItem>
      ))}

      {state.partial ? <Muted>{`› ${state.partial}`}</Muted> : null}
    </Screen>
  );
}
