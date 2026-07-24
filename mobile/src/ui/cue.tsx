/**
 * Cue UI for the mobile app (XERK-81) — parity with the web SPA's cue surfaces:
 * the global aggressiveness toggle + live band on the Live screen, and the inline
 * cue dropdown in history. Themed via the shared ThemeContext.
 */

import type { CueLevel, LiveCue } from "@tenir/client-core";
import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { CUE_LEVELS } from "../storage";
import { useThemedStyles } from "./ThemeContext";
import { radius, space, withAlpha, type Palette } from "./theme";

const LEVEL_LABEL: Record<CueLevel, string> = {
  conservative: "Conservative",
  balanced: "Balanced",
  aggressive: "Aggressive",
};

/** Global toggle for how eagerly private context cues appear. */
export function CueLevelToggle({
  level,
  onChange,
}: {
  level: CueLevel;
  onChange: (l: CueLevel) => void;
}): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return (
    <View style={styles.toggle} accessibilityRole="radiogroup" accessibilityLabel="Cue detail level">
      <Text style={styles.toggleCaption}>Cues</Text>
      {CUE_LEVELS.map((l) => {
        const active = l === level;
        return (
          <Pressable
            key={l}
            accessibilityRole="radio"
            accessibilityState={{ selected: active }}
            accessibilityLabel={LEVEL_LABEL[l]}
            onPress={() => onChange(l)}
            style={[styles.option, active && styles.optionActive]}
          >
            <Text style={[styles.optionText, active && styles.optionTextActive]}>
              {LEVEL_LABEL[l]}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

/**
 * The single active cue above the transcript (XERK-102). One cue shows at a
 * time; others wait in a FIFO queue and pop the moment this one is released. A
 * "+N more" note appears while cues are queued behind it.
 */
export function LiveCueBand({
  activeCue,
  queuedCount,
}: {
  activeCue: LiveCue | null;
  queuedCount: number;
}): JSX.Element | null {
  const styles = useThemedStyles(makeStyles);
  if (!activeCue) return null;
  return (
    <View style={styles.band}>
      <View style={styles.card}>
        <Text style={styles.cardTitle}>{activeCue.title.toUpperCase()}</Text>
        {/* Cue text is selectable so it can be copied (XERK-104). */}
        <Text selectable style={styles.cardBody}>
          {activeCue.body}
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
    </View>
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
    toggle: { flexDirection: "row", alignItems: "center", gap: space.xs, flexWrap: "wrap" },
    toggleCaption: {
      color: colors.muted,
      fontSize: 11,
      textTransform: "uppercase",
      letterSpacing: 0.5,
    },
    option: {
      borderColor: colors.border,
      borderWidth: 1,
      borderRadius: radius.sm,
      paddingHorizontal: space.sm,
      paddingVertical: 4,
    },
    optionActive: { borderColor: colors.accent, backgroundColor: withAlpha(colors.accent, 0.14) },
    optionText: { color: colors.muted, fontSize: 11, fontWeight: "600" },
    optionTextActive: { color: colors.accentStrong },
    band: { gap: space.sm },
    card: {
      borderColor: colors.accent,
      borderWidth: 1,
      borderRadius: radius.md,
      backgroundColor: withAlpha(colors.accent, 0.14),
      padding: space.md,
      gap: 2,
    },
    cardTitle: { color: colors.accentStrong, fontWeight: "700", fontSize: 12, letterSpacing: 0.5 },
    cardBody: { color: colors.text, lineHeight: 20 },
    queued: { color: colors.muted, fontSize: 12, fontWeight: "600" },
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
