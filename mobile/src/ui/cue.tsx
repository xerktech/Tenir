/**
 * Cue UI for the mobile app (XERK-81) — parity with the web SPA's cue surfaces:
 * the global aggressiveness toggle + live band on the Live screen, and the inline
 * clickable box + detail popup in history. Themed via the shared ThemeContext.
 */

import type { CueLevel, LiveCue } from "@tenir/client-core";
import { Modal, Pressable, StyleSheet, Text, View } from "react-native";

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
        <Text style={styles.cardBody}>{activeCue.body}</Text>
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

/** An inline clickable cue in the history transcript; opens the detail popup. */
export function InlineCue({ title, onPress }: { title: string; onPress: () => void }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Cue: ${title}`}
      onPress={onPress}
      style={styles.inline}
    >
      <Text style={styles.inlineText}>✦ {title}</Text>
    </Pressable>
  );
}

/** The cue detail popup — a modal, not a new screen. */
export function CueModal({
  title,
  body,
  onClose,
}: {
  title: string;
  body: string;
  onClose: () => void;
}): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return (
    <Modal transparent animationType="fade" visible onRequestClose={onClose}>
      <Pressable style={styles.backdrop} onPress={onClose}>
        <Pressable style={styles.modal} onPress={() => {}}>
          <View style={styles.modalHead}>
            <Text style={styles.modalTitle}>{title}</Text>
            <Pressable accessibilityRole="button" accessibilityLabel="Close" onPress={onClose}>
              <Text style={styles.modalClose}>✕</Text>
            </Pressable>
          </View>
          <Text style={styles.modalBody}>{body}</Text>
        </Pressable>
      </Pressable>
    </Modal>
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
    inline: {
      alignSelf: "flex-start",
      borderColor: colors.accent,
      borderWidth: 1,
      borderRadius: radius.sm,
      backgroundColor: withAlpha(colors.accent, 0.14),
      paddingHorizontal: space.sm,
      paddingVertical: 4,
      marginVertical: space.xs,
    },
    inlineText: { color: colors.accentStrong, fontWeight: "600" },
    backdrop: {
      flex: 1,
      backgroundColor: "rgba(0,0,0,0.5)",
      alignItems: "center",
      justifyContent: "center",
      padding: space.lg,
    },
    modal: {
      width: "100%",
      maxWidth: 420,
      backgroundColor: colors.surfaceRaised,
      borderColor: colors.border,
      borderWidth: 1,
      borderRadius: radius.lg,
      padding: space.lg,
      gap: space.sm,
    },
    modalHead: { flexDirection: "row", alignItems: "center", gap: space.sm },
    modalTitle: { color: colors.text, fontSize: 18, fontWeight: "700", flexGrow: 1 },
    modalClose: { color: colors.muted, fontSize: 16 },
    modalBody: { color: colors.text, lineHeight: 22 },
  });
