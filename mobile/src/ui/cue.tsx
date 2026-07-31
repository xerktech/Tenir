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
  cueSecondsUntil,
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
 * cancels the pending unmount and takes over immediately, matching the "one cue
 * at a time, freshest wins" rule (XERK-102, XERK-159). Mirrors the web `useCueExit`.
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
 * `useCueCountdown`: derived from the cue's wall-clock end time (`activeCueEndsAt`,
 * XERK-159) against the real clock rather than decremented per tick, so a
 * backgrounded app resyncs to the truth instead of drifting away from the release
 * timer — a cue whose turn opened while away shows the time it actually has left,
 * not a fresh ten. It restarts for each cue that takes the band and holds its last
 * value through the exit fade (when `endsAt` is null).
 */
function useCueCountdown(endsAt: number | null): number {
  const [secondsLeft, setSecondsLeft] = useState(() => cueSecondsLeft(0));
  useEffect(() => {
    if (endsAt == null) return;
    const tick = () => setSecondsLeft(cueSecondsUntil(endsAt, Date.now()));
    tick();
    const timer = setInterval(tick, CUE_COUNTDOWN_TICK_MS);
    return () => clearInterval(timer);
  }, [endsAt]);
  return secondsLeft;
}

/**
 * The single active cue over the transcript (XERK-102). One cue shows at a time
 * for its full turn; others wait in a FIFO queue and take the band as each turn
 * ends. The turns run on a continuous wall-clock schedule that keeps advancing
 * while the app is backgrounded (XERK-159), so a cue whose turn passed while you
 * were away is already in the transcript on return rather than replaying — you
 * rejoin the live cue mid-turn, not a backlog. A "+N more" note appears while
 * cues are queued behind it.
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
  activeCueEndsAt,
  queuedCount,
}: {
  activeCue: LiveCue | null;
  activeCueEndsAt: number | null;
  queuedCount: number;
}): JSX.Element | null {
  const styles = useThemedStyles(makeStyles);
  const { cue, exiting } = useCueExit(activeCue);
  const secondsLeft = useCueCountdown(activeCueEndsAt);
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
          <Text style={styles.cardTitle}>{cue.title}</Text>
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
        {/* Where the fact came from (XERK-120): the live source it was grounded
            in. Absent for a cue from the model's own knowledge. */}
        {cue.source ? <Text style={styles.cardSource}>{cue.source}</Text> : null}
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
export function CueDisclosure({
  title,
  body,
  source,
}: {
  title: string;
  body: string;
  /** Live-source attribution (XERK-120), shown under the body when present. */
  source?: string | null;
}): JSX.Element {
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
        <View style={styles.disclosureBody}>
          <Text selectable style={styles.disclosureText}>
            {body}
          </Text>
          {source ? <Text style={styles.disclosureSource}>{source}</Text> : null}
        </View>
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
      // A long title wraps rather than shoving the countdown off the card.
      flexShrink: 1,
    },
    // Quieter than the title: a hint about how long the card lingers, not part
    // of the cue itself.
    cardCountdown: { color: colors.muted, fontWeight: "600", fontSize: 11, flexShrink: 0 },
    cardBody: { color: colors.text, lineHeight: 20 },
    // Live-source attribution under the body (XERK-120): provenance, not content.
    cardSource: { color: colors.muted, fontWeight: "600", fontSize: 11, letterSpacing: 0.2 },
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
    // Container visuals on the View; text styling on disclosureText (the body
    // became a View so the source line can sit under the text, XERK-120).
    disclosureBody: {
      marginTop: space.xs,
      paddingVertical: space.sm,
      paddingHorizontal: space.md,
      borderLeftColor: colors.accent,
      borderLeftWidth: 2,
      backgroundColor: withAlpha(colors.accent, 0.14),
      borderRadius: radius.sm,
      gap: 2,
    },
    disclosureText: { color: colors.text, lineHeight: 22 },
    disclosureSource: { color: colors.muted, fontWeight: "600", fontSize: 11, letterSpacing: 0.2 },
  });
