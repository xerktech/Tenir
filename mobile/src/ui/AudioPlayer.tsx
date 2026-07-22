/**
 * In-app audio player (XERK-67): a play/pause control, a live m:ss / m:ss clock,
 * and a touch-draggable seek bar for a retained conversation clip. Presentational
 * — all playback state and the native handoff live in `lib/useAudioPlayer`.
 *
 * The seek bar is built from plain Views + a `PanResponder` (no slider dependency,
 * matching the app's zero-heavy-dep native modules): we measure the track width on
 * layout and map a touch's x-offset within it to a 0..1 scrub position.
 */

import { useRef } from "react";
import {
  type GestureResponderEvent,
  type LayoutChangeEvent,
  PanResponder,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { useAudioPlayer } from "../lib/useAudioPlayer";
import { msToClock } from "../lib/format";
import { progressFraction, scrubFraction } from "../lib/audioPlayer";
import { Button } from "./components";
import { colors, space } from "./theme";

export function AudioPlayer({ url }: { url: string }): JSX.Element {
  const { state, toggle, seekToFraction } = useAudioPlayer(url);
  const width = useRef(0);

  const onLayout = (e: LayoutChangeEvent) => {
    width.current = e.nativeEvent.layout.width;
  };

  // Map a touch anywhere on the track to a 0..1 position and scrub to it — the
  // initial tap and every drag move both route through here, so a tap jumps and a
  // drag scrubs.
  const scrubTo = (e: GestureResponderEvent) => {
    const fraction = scrubFraction(e.nativeEvent.locationX, width.current);
    if (fraction !== null) seekToFraction(fraction);
  };

  // The PanResponder is created once, so its handlers must not close over this
  // render's scrubTo — that one captured seekToFraction while the clip's duration
  // was still 0, which made every touch seek to the start (a restart) instead of
  // scrubbing. Route through a ref that always holds the latest scrubTo.
  const scrubRef = useRef(scrubTo);
  scrubRef.current = scrubTo;

  const responder = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => true,
      onMoveShouldSetPanResponder: () => true,
      onPanResponderGrant: (e) => scrubRef.current(e),
      onPanResponderMove: (e) => scrubRef.current(e),
    }),
  ).current;

  const fraction = progressFraction(state);
  const playing = state.status === "playing";

  return (
    <View style={styles.container}>
      <View style={styles.row}>
        <Button
          title={playing ? "❚❚ Pause" : "▶ Play"}
          kind="primary"
          onPress={toggle}
          disabled={state.status === "loading" || state.status === "error"}
        />
        <Text style={styles.clock}>
          {msToClock(state.positionMs)} / {msToClock(state.durationMs)}
        </Text>
      </View>

      {state.status === "error" ? (
        <Text style={styles.error}>Couldn’t play this recording.</Text>
      ) : (
        <View
          style={styles.track}
          onLayout={onLayout}
          accessibilityRole="adjustable"
          accessibilityLabel="Seek"
          accessibilityValue={{ min: 0, max: 100, now: Math.round(fraction * 100) }}
          {...responder.panHandlers}
        >
          {/* Base rule (full width) → played fill → draggable thumb, stacked. The
              bars are non-interactive so a touch always lands on the track and its
              locationX is measured against the full width, not a child. */}
          <View style={styles.base} pointerEvents="none" />
          <View style={[styles.fill, { width: `${fraction * 100}%` }]} pointerEvents="none" />
          {/* `marginLeft: -6` centres the 12px thumb on the fill's leading edge. */}
          <View style={[styles.thumb, { left: `${fraction * 100}%` }]} pointerEvents="none" />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { gap: space.sm, marginVertical: space.sm },
  row: { flexDirection: "row", alignItems: "center", gap: space.md },
  clock: { color: colors.muted, fontSize: 13, fontVariant: ["tabular-nums"] },
  error: { color: colors.danger, fontSize: 13 },
  // A tall touch target around a thin visible track so the whole strip is grabbable.
  track: { height: 28, justifyContent: "center" },
  base: {
    position: "absolute",
    left: 0,
    right: 0,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.border,
  },
  fill: {
    position: "absolute",
    left: 0,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.accent,
  },
  thumb: {
    position: "absolute",
    width: 12,
    height: 12,
    borderRadius: 6,
    marginLeft: -6,
    backgroundColor: colors.accent,
  },
});
