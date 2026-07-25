/**
 * Cue UI for the mobile app (XERK-81) — parity with the web SPA's cue surfaces:
 * the live band on the Live screen and the inline cue dropdown in history. Themed
 * via the shared ThemeContext.
 */

import {
  CUE_COUNTDOWN_TICK_MS,
  CUE_EXIT_MS,
  cueCountdownLabel,
  cueSecondsLeft,
  type LiveCue,
} from "@tenir/client-core";
import { useEffect, useRef, useState } from "react";
import { Animated, Pressable, StyleSheet, Text, View } from "react-native";

import { useThemedStyles } from "./ThemeContext";
import { mix, radius, space, withAlpha, type Palette } from "./theme";

/**
 * Keep a released cue mounted for the length of its fade-out (XERK-107).
 *
 * Returns the cue to paint plus whether it is on its way out, so the band can
 * fade to nothing instead of blinking off the screen. A cue arriving mid-fade
 * cancels the pending unmount and takes over immediately, matching the "only
 * one cue at a time" rule (XERK-102). Mirrors the web `useCueExit`.
 */
function useCueExit(activeCue: LiveCue | null): { cue: LiveCue | null; exiting: boolean } {
  const [painted, setPainted] = useState<LiveCue | null>(activeCue);
  useEffect(() => {
    if (activeCue) {
      setPainted(activeCue);
      return;
    }
    const timer = setTimeout(() => setPainted(null), CUE_EXIT_MS);
    return () => clearTimeout(timer);
  }, [activeCue]);
  return { cue: painted, exiting: !activeCue && painted != null };
}

/**
 * Count the seconds a cue has left on screen (XERK-110). Mirrors the web
 * `useCueCountdown`: derived from the moment the cue appeared rather than
 * decremented per tick, so a backgrounded app resyncs to the truth instead of
 * drifting away from the release timer, and restarts for each cue that takes
 * the band — a promoted cue gets its own full countdown.
 */
function useCueCountdown(cueId: string | undefined): number {
  const [secondsLeft, setSecondsLeft] = useState(() => cueSecondsLeft(0));
  useEffect(() => {
    if (!cueId) return;
    setSecondsLeft(cueSecondsLeft(0));
    const startedAt = Date.now();
    const timer = setInterval(
      () => setSecondsLeft(cueSecondsLeft(Date.now() - startedAt)),
      CUE_COUNTDOWN_TICK_MS,
    );
    return () => clearInterval(timer);
  }, [cueId]);
  return secondsLeft;
}

/**
 * The single active cue over the transcript (XERK-102). One cue shows at a
 * time; others wait in a FIFO queue and pop the moment this one is released. A
 * "+N more" note appears while cues are queued behind it.
 *
 * The band *floats over* the top of the transcript rather than sitting above it
 * in the column (XERK-107): as a flow element, each arrival shoved the
 * transcript down and each expiry let it snap back, which is disorienting
 * mid-conversation. As an overlay it changes no layout at all — it briefly
 * covers the oldest visible captions instead, which are the ones nobody is
 * reading, since the transcript follows the newest text at the bottom. The band
 * is `box-none` so the transcript underneath still scrolls; the card itself
 * keeps its touches so its text stays long-pressable.
 */
export function LiveCueBand({
  activeCue,
  queuedCount,
}: {
  activeCue: LiveCue | null;
  queuedCount: number;
}): JSX.Element | null {
  const styles = useThemedStyles(makeStyles);
  const { cue, exiting } = useCueExit(activeCue);
  const secondsLeft = useCueCountdown(cue?.id);
  const fade = useRef(new Animated.Value(0)).current;
  const shown = Boolean(cue) && !exiting;
  useEffect(() => {
    Animated.timing(fade, {
      toValue: shown ? 1 : 0,
      duration: CUE_EXIT_MS,
      useNativeDriver: true,
    }).start();
  }, [shown, fade]);
  if (!cue) return null;
  return (
    <Animated.View
      pointerEvents="box-none"
      style={[
        styles.band,
        {
          opacity: fade,
          transform: [{ translateY: fade.interpolate({ inputRange: [0, 1], outputRange: [-4, 0] }) }],
        },
      ]}
    >
      <View style={styles.card}>
        <View style={styles.cardHead}>
          <Text style={styles.cardTitle}>{cue.title.toUpperCase()}</Text>
          {/* Seconds until the cue auto-dismisses (XERK-110), across from the
              title. Kept out of the accessibility tree: a number that changes
              every second would otherwise be read out over the cue itself. */}
          <Text
            style={styles.cardCountdown}
            importantForAccessibility="no-hide-descendants"
            accessibilityElementsHidden
          >
            {cueCountdownLabel(secondsLeft)}
          </Text>
        </View>
        {/* Cue text is selectable so it can be copied (XERK-104). */}
        <Text selectable style={styles.cardBody}>
          {cue.body}
        </Text>
      </View>
      {queuedCount > 0 && (
        <Text
          style={styles.queued}
          accessibilityLabel={`${queuedCount} more ${queuedCount === 1 ? "cue" : "cues"} queued`}
        >
          +{queuedCount} more
        </Text>
      )}
    </Animated.View>
  );
}

/**
 * An inline cue in the history transcript: a collapsed dropdown that expands in
 * place to reveal the body (XERK-105). It replaced a click-through modal so the
 * detail reads on the timeline; it defaults to minimized to keep the transcript
 * scannable.
 */
export function CueDisclosure({ title, body }: { title: string; body: string }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  const [open, setOpen] = useState(false);
  return (
    <View style={styles.disclosure}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Cue: ${title}`}
        accessibilityState={{ expanded: open }}
        onPress={() => setOpen((o) => !o)}
        style={styles.inline}
      >
        <Text style={styles.inlineText}>
          {open ? "▾" : "▸"} ✦ {title}
        </Text>
      </Pressable>
      {/* Cue text is selectable so it can be copied (XERK-104). */}
      {open && (
        <Text selectable style={styles.disclosureBody}>
          {body}
        </Text>
      )}
    </View>
  );
}

const makeStyles = (colors: Palette) =>
  StyleSheet.create({
    // Floats over the transcript's top edge instead of displacing it (XERK-107).
    band: { position: "absolute", top: 0, left: 0, right: 0, zIndex: 2, gap: space.sm },
    card: {
      borderColor: colors.accent,
      borderWidth: 1,
      borderRadius: radius.md,
      // Opaque (accent wash blended into the raised surface) and lifted, so the
      // captions it covers can't read through it.
      backgroundColor: mix(colors.accent, colors.surfaceRaised, 0.14),
      elevation: 4,
      padding: space.md,
      gap: 2,
    },
    // Title on the left, countdown on the right (XERK-110).
    cardHead: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      gap: space.sm,
    },
    cardTitle: {
      color: colors.accentStrong,
      fontWeight: "700",
      fontSize: 12,
      letterSpacing: 0.5,
      // A long title wraps rather than shoving the countdown off the card.
      flexShrink: 1,
    },
    // Quieter than the title: a hint about how long the card lingers, not part
    // of the cue itself.
    cardCountdown: { color: colors.muted, fontWeight: "600", fontSize: 11, flexShrink: 0 },
    cardBody: { color: colors.text, lineHeight: 20 },
    // Chipped like the card, since it now sits over the captions too.
    queued: {
      alignSelf: "flex-start",
      color: colors.muted,
      fontSize: 12,
      fontWeight: "600",
      backgroundColor: colors.surfaceRaised,
      borderColor: colors.border,
      borderWidth: 1,
      borderRadius: radius.sm,
      paddingHorizontal: space.sm,
      elevation: 2,
    },
    disclosure: { alignSelf: "flex-start", marginVertical: space.xs },
    inline: {
      alignSelf: "flex-start",
      borderColor: colors.accent,
      borderWidth: 1,
      borderRadius: radius.sm,
      backgroundColor: withAlpha(colors.accent, 0.14),
      paddingHorizontal: space.sm,
      paddingVertical: 4,
    },
    inlineText: { color: colors.accentStrong, fontWeight: "600" },
    disclosureBody: {
      color: colors.text,
      lineHeight: 22,
      marginTop: space.xs,
      paddingVertical: space.sm,
      paddingHorizontal: space.md,
      borderLeftColor: colors.accent,
      borderLeftWidth: 2,
      backgroundColor: withAlpha(colors.accent, 0.14),
      borderRadius: radius.sm,
    },
  });
