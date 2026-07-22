/**
 * Cue UI for the mobile app (XERK-81) — parity with the web SPA's cue surfaces:
 * the global aggressiveness toggle + live band on the Live screen, and the inline
 * clickable box + detail popup in history.
 */

import type { CueLevel, LiveCue } from "@tenir/client-core";
import { Modal, Pressable, StyleSheet, Text, View } from "react-native";

import { CUE_LEVELS } from "../storage";
import { colors, space } from "./theme";

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

/** The live band of cues above the transcript (auto-dismissed by the core). */
export function LiveCueBand({ cues }: { cues: LiveCue[] }): JSX.Element | null {
  if (cues.length === 0) return null;
  return (
    <View style={styles.band}>
      {cues.map((c) => (
        <View key={c.id} style={styles.card}>
          <Text style={styles.cardTitle}>{c.title.toUpperCase()}</Text>
          <Text style={styles.cardBody}>{c.body}</Text>
        </View>
      ))}
    </View>
  );
}

/** An inline clickable cue in the history transcript; opens the detail popup. */
export function InlineCue({ title, onPress }: { title: string; onPress: () => void }): JSX.Element {
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

const styles = StyleSheet.create({
  toggle: { flexDirection: "row", alignItems: "center", gap: space.xs, flexWrap: "wrap" },
  toggleCaption: { color: colors.muted, fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5 },
  option: {
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: space.sm,
    paddingVertical: 4,
  },
  optionActive: { borderColor: colors.accent, backgroundColor: colors.card },
  optionText: { color: colors.muted, fontSize: 11, fontWeight: "600" },
  optionTextActive: { color: colors.accent },
  band: { gap: space.sm },
  card: {
    borderColor: colors.accent,
    borderWidth: 1,
    borderRadius: 10,
    backgroundColor: colors.card,
    padding: space.md,
    gap: 2,
  },
  cardTitle: { color: colors.accent, fontWeight: "700", fontSize: 12, letterSpacing: 0.5 },
  cardBody: { color: colors.text, lineHeight: 20 },
  inline: {
    alignSelf: "flex-start",
    borderColor: colors.accent,
    borderWidth: 1,
    borderRadius: 8,
    backgroundColor: colors.card,
    paddingHorizontal: space.sm,
    paddingVertical: 4,
    marginVertical: space.xs,
  },
  inlineText: { color: colors.accent, fontWeight: "600" },
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
    backgroundColor: colors.card,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 14,
    padding: space.lg,
    gap: space.sm,
  },
  modalHead: { flexDirection: "row", alignItems: "center", gap: space.sm },
  modalTitle: { color: colors.text, fontSize: 18, fontWeight: "700", flexGrow: 1 },
  modalClose: { color: colors.muted, fontSize: 16 },
  modalBody: { color: colors.text, lineHeight: 22 },
});
