/**
 * Live capture: phone-mic transcription rendered on the phone screen.
 *
 * Tap Start to stream the phone mic to the api and watch captions land live.
 * Pause stops uploading without dropping the session; Stop ends it.
 *
 * A thin presenter over `useCapture`: all the session behaviour lives in the tested,
 * framework-agnostic `CaptureSession`.
 */

import { useEffect, useRef, useState } from "react";
import {
  ScrollView,
  StyleSheet,
  Text,
  View,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";

import {
  DISCLOSURES,
  isPinnedToBottom,
  liveTranscript,
  type CaptureSegment,
  type CueLevel,
  type LiveCue,
  type LiveTranscriptItem,
} from "@tenir/client-core";
import { useCapture } from "../lib/useCapture";
import { useNotify } from "../lib/notify";
import { deviceKeyValue } from "../secureStorage";
import { loadCueLevel, saveCueLevel } from "../storage";
import { CueDisclosure, CueLevelToggle, LiveCueBand } from "../ui/cue";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Heading,
  Muted,
  Row,
} from "../ui/components";
import { useTheme, useThemedStyles } from "../ui/ThemeContext";
import { space, type Palette } from "../ui/theme";

const RECORDING_NOTICE = DISCLOSURES.find((d) => d.id === "recording")?.body ?? "";

// Consecutive turns are grouped into a run so they render inside one selectable
// <Text> — RN only extends a text selection within a single <Text> tree, so a
// run is the largest span a drag-select can cover (XERK-104). A released cue is
// a separate tappable dropdown (XERK-108), so it ends the current run — the same
// grouping the History screen uses.
type LiveRun = { kind: "segments"; segs: CaptureSegment[] } | { kind: "cue"; cue: LiveCue };

function liveRuns(items: LiveTranscriptItem[]): LiveRun[] {
  const out: LiveRun[] = [];
  for (const item of items) {
    if (item.kind === "cue") {
      out.push({ kind: "cue", cue: item.cue });
    } else {
      const last = out[out.length - 1];
      if (last && last.kind === "segments") last.segs.push(item.segment);
      else out.push({ kind: "segments", segs: [item.segment] });
    }
  }
  return out;
}

function connectionLabel(state: ReturnType<typeof useCapture>["state"]): string {
  if (!state.running) return "idle";
  if (state.connection === "open") return state.listening ? "● live" : "❚❚ paused";
  if (state.connection === "connecting") return "… connecting";
  return "× reconnecting";
}

export function LiveScreen({ wsUrl }: { wsUrl: string }): JSX.Element {
  const [cueLevel, setCueLevel] = useState<CueLevel>("balanced");
  const cap = useCapture(wsUrl, cueLevel);
  const notify = useNotify();
  const { colors } = useTheme();
  const styles = useThemedStyles(makeStyles);
  const [busy, setBusy] = useState(false);
  const { state } = cap;

  // The transcript scrolls inside its own box below the fixed controls + cue
  // band, so a long session never scrolls them off-screen (XERK-103). Follow
  // the newest caption while the viewer is at the bottom; release once they
  // scroll up to re-read (shared geometry with the web + even companion).
  const scrollRef = useRef<ScrollView>(null);
  const pinnedRef = useRef(true);
  const onTranscriptScroll = (e: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = e.nativeEvent;
    pinnedRef.current = isPinnedToBottom({
      scrollTop: contentOffset.y,
      scrollHeight: contentSize.height,
      clientHeight: layoutMeasurement.height,
    });
  };
  const followNewest = () => {
    if (pinnedRef.current) scrollRef.current?.scrollToEnd({ animated: false });
  };

  // Hydrate the persisted cue-level preference once on mount.
  useEffect(() => {
    let live = true;
    void loadCueLevel(deviceKeyValue()).then((l) => live && setCueLevel(l));
    return () => {
      live = false;
    };
  }, []);

  const changeCueLevel = (l: CueLevel) => {
    setCueLevel(l);
    void saveCueLevel(deviceKeyValue(), l);
  };

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

  const hasContent =
    state.segments.length > 0 || state.pastCues.length > 0 || Boolean(state.partial);

  // Interleave finalized turns with cues already released from the band so a past
  // cue can be re-read inline without disturbing the cues still coming in above
  // (XERK-108), then group consecutive turns into selectable runs (XERK-104) —
  // the same segment/cue timeline the history screen renders.
  const runList = liveRuns(liveTranscript(state.segments, state.pastCues));
  const lastIsSegments = runList[runList.length - 1]?.kind === "segments";

  return (
    // A fixed column — controls + cue band stay put — over a transcript that
    // scrolls on its own, so the cues above it are never scrolled away (XERK-103).
    <View style={styles.screen}>
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
        <CueLevelToggle level={cueLevel} onChange={changeCueLevel} />
      </Card>

      <LiveCueBand activeCue={state.activeCue} queuedCount={state.queuedCues.length} />

      {!hasContent && !state.running ? (
        <EmptyState title="No captions yet." hint="Press Start to begin a live conversation." />
      ) : null}
      {!hasContent && state.running ? <Muted>Listening…</Muted> : null}

      {hasContent ? (
        <ScrollView
          ref={scrollRef}
          style={styles.transcript}
          contentContainerStyle={styles.transcriptContent}
          onScroll={onTranscriptScroll}
          scrollEventThrottle={100}
          onContentSizeChange={followNewest}
        >
          {/* Each run of consecutive turns is ONE selectable <Text>, so a
              selection can be dragged across every turn in it and copied
              (XERK-104). A released cue is a separate dropdown that ends the run
              (XERK-108); selecting across a cue is a documented platform gap vs.
              the web/even DOM clients. The live partial rides the trailing turn
              run so it's selectable too. */}
          {runList.map((run, i) =>
            run.kind === "cue" ? (
              <CueDisclosure key={`cue-${run.cue.id}`} title={run.cue.title} body={run.cue.body} />
            ) : (
              <Text key={`run-${i}`} selectable style={{ color: colors.text, lineHeight: 22 }}>
                {run.segs.map((seg, j) => (
                  <Text key={seg.id}>
                    {j > 0 ? "\n" : null}
                    {seg.text}
                  </Text>
                ))}
                {state.partial && i === runList.length - 1 ? (
                  <Text style={{ color: colors.muted }}>
                    {run.segs.length > 0 ? "\n" : null}
                    {`› ${state.partial}`}
                  </Text>
                ) : null}
              </Text>
            ),
          )}
          {/* A partial with no trailing turn run to ride (a cue is last, or no
              turns yet) gets its own selectable line. */}
          {state.partial && !lastIsSegments ? (
            <Text selectable style={{ color: colors.muted, lineHeight: 22 }}>{`› ${state.partial}`}</Text>
          ) : null}
        </ScrollView>
      ) : null}
    </View>
  );
}

const makeStyles = (colors: Palette) =>
  StyleSheet.create({
    // Mirror the shared `Screen` padding/background, but as a fixed column so
    // only the transcript below scrolls (the cue band above stays in view).
    screen: { flex: 1, backgroundColor: colors.bg, padding: space.lg, gap: space.md },
    transcript: { flex: 1 },
    transcriptContent: { gap: space.xs, paddingBottom: space.md },
  });
