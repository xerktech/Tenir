/**
 * A tiny React Native component kit so the screens read like the web SPA's markup —
 * `Screen`/`Card`/`Field`/`Button`/`ListItem`/`Badge`/`EmptyState` instead of raw
 * `View`/`Text`/`Pressable`. Presentational only; all behaviour lives in the
 * container hooks (`lib/controllers`).
 *
 * Styling follows the shared Lumen/Turma conventions (see docs/design-language.md):
 * quiet outline buttons + one solid primary, pill badges tinted from the semantic
 * colour, dashed-border empty states, arm-then-confirm destructive controls, and
 * uppercase micro-labels. All colours come from the active theme palette.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ActivityIndicator,
  Animated,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { createConfirmArmer } from "../lib/confirm";
import { useTheme, useThemedStyles } from "./ThemeContext";
import { radius, space, withAlpha, type Palette } from "./theme";

export function Screen({ children }: { children: ReactNode }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.screenContent}>
      {children}
    </ScrollView>
  );
}

export function Heading({ children }: { children: ReactNode }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return <Text style={styles.heading}>{children}</Text>;
}

export function Muted({ children }: { children: ReactNode }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return <Text style={styles.muted}>{children}</Text>;
}

/** Uppercase micro-label — section/field captions (the web's `.field-label`). */
export function Label({ children }: { children: ReactNode }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return <Text style={styles.label}>{children}</Text>;
}

export function Card({ children }: { children: ReactNode }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return <View style={styles.card}>{children}</View>;
}

export function Row({ children }: { children: ReactNode }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return <View style={styles.row}>{children}</View>;
}

export function Field(props: {
  placeholder: string;
  value: string;
  onChangeText: (t: string) => void;
  /** Optional uppercase micro-label above the input (the web `Field` pattern). */
  label?: string;
  secureTextEntry?: boolean;
  multiline?: boolean;
}): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  const { colors } = useTheme();
  const input = (
    <TextInput
      style={[styles.field, props.multiline ? styles.fieldMultiline : null]}
      placeholder={props.placeholder}
      placeholderTextColor={colors.muted}
      value={props.value}
      onChangeText={props.onChangeText}
      secureTextEntry={props.secureTextEntry}
      multiline={props.multiline}
      autoCapitalize="none"
    />
  );
  if (!props.label) return input;
  return (
    <View style={styles.labelledField}>
      <Label>{props.label}</Label>
      {input}
    </View>
  );
}

type ButtonKind = "default" | "primary" | "danger";

export function Button(props: {
  title: string;
  onPress: () => void;
  kind?: ButtonKind;
  disabled?: boolean;
}): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  const kind = props.kind ?? "default";
  return (
    <Pressable
      accessibilityRole="button"
      disabled={props.disabled}
      onPress={props.onPress}
      style={({ pressed }) => [
        styles.button,
        kind === "primary" && styles.buttonPrimary,
        kind === "danger" && styles.buttonDanger,
        (props.disabled || pressed) && styles.buttonDim,
      ]}
    >
      <Text
        style={[
          styles.buttonText,
          kind === "primary" && styles.buttonTextPrimary,
          kind === "danger" && styles.buttonTextDanger,
        ]}
      >
        {props.title}
      </Text>
    </Pressable>
  );
}

/**
 * Two-step destructive button (Turma's arm-then-confirm pattern, matching the
 * web `ConfirmButton`): the first press arms it — the outline fills solid and
 * shows `confirmTitle` — and only a second press within the window commits.
 * The armed state quietly expires. Replaces confirmation dialogs.
 */
export function ConfirmButton(props: {
  /** Resting label, e.g. "Delete". */
  title: string;
  /** Armed label naming the commitment, e.g. "Confirm delete". */
  confirmTitle: string;
  /** Fired by the second (confirming) press. */
  onConfirm: () => void;
  disabled?: boolean;
}): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  const [armed, setArmed] = useState(false);
  const onConfirmRef = useRef(props.onConfirm);
  onConfirmRef.current = props.onConfirm;
  const armer = useMemo(
    () => createConfirmArmer({ onConfirm: () => onConfirmRef.current(), onChange: setArmed }),
    [],
  );
  useEffect(() => () => armer.disarm(), [armer]);

  return (
    <Pressable
      accessibilityRole="button"
      disabled={props.disabled}
      onPress={() => armer.fire()}
      style={({ pressed }) => [
        styles.button,
        styles.buttonDanger,
        armed && styles.buttonDangerArmed,
        (props.disabled || pressed) && styles.buttonDim,
      ]}
    >
      <Text style={[styles.buttonText, armed ? styles.buttonTextDangerArmed : styles.buttonTextDanger]}>
        {armed ? props.confirmTitle : props.title}
      </Text>
    </Pressable>
  );
}

export function ListItem({ children }: { children: ReactNode }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return <View style={styles.item}>{children}</View>;
}

export function Spinner({ label }: { label?: string }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  const { colors } = useTheme();
  if (!label) return <ActivityIndicator color={colors.accent} style={styles.spinner} />;
  return (
    <View style={styles.spinnerRow}>
      <ActivityIndicator color={colors.accent} />
      <Muted>{label}</Muted>
    </View>
  );
}

type BadgeTone = "accent" | "neutral";

/**
 * A fully-round tinted pill (Turma's chip formula: the semantic colour at 45%
 * for the border and 10% for the fill, full strength for the text) — matches
 * the web `Badge`.
 */
export function Badge({
  children,
  tone = "accent",
}: {
  children: ReactNode;
  tone?: BadgeTone;
}): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return (
    <View style={[styles.badge, tone === "accent" ? styles.badgeAccent : styles.badgeNeutral]}>
      <Text style={tone === "accent" ? styles.badgeTextAccent : styles.badgeTextNeutral}>
        {children}
      </Text>
    </View>
  );
}

/** Dashed-border "nothing here yet" block — matches the web `EmptyState`. */
export function EmptyState({ title, hint }: { title: string; hint?: string }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  return (
    <View style={styles.empty}>
      <Text style={styles.emptyTitle}>{title}</Text>
      {hint ? <Text style={styles.emptyHint}>{hint}</Text> : null}
    </View>
  );
}

type DotState = "ready" | "connecting" | "down";

/**
 * A status "light" — a small filled dot coloured by component state, pulsing
 * gently while connecting (the web's `.status-dot` counterpart; RN has no
 * box-shadow glow, a documented platform exception).
 */
export function StatusDot({ state }: { state: DotState }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  const { colors } = useTheme();
  const dotColor: Record<DotState, string> = {
    ready: colors.success,
    connecting: colors.warning,
    down: colors.danger,
  };
  const opacity = usePulse(state === "connecting");
  return (
    <Animated.View
      accessibilityRole="image"
      accessibilityLabel={state}
      style={[styles.dot, { backgroundColor: dotColor[state], opacity }]}
    />
  );
}

/** 1 ↔ 0.35 opacity loop while `active` (mirrors the web's status-pulse). */
function usePulse(active: boolean): Animated.Value {
  const value = useRef(new Animated.Value(1)).current;
  useEffect(() => {
    if (!active) {
      value.setValue(1);
      return;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(value, { toValue: 0.35, duration: 700, useNativeDriver: true }),
        Animated.timing(value, { toValue: 1, duration: 700, useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [active, value]);
  return value;
}

const makeStyles = (colors: Palette) =>
  StyleSheet.create({
    screen: { flex: 1, backgroundColor: colors.bg },
    screenContent: { padding: space.lg, gap: space.md },
    heading: {
      color: colors.text,
      fontSize: 20,
      fontWeight: "700",
      letterSpacing: -0.2,
      marginBottom: space.sm,
    },
    muted: { color: colors.muted, fontSize: 13 },
    label: {
      color: colors.muted,
      fontSize: 11,
      fontWeight: "600",
      textTransform: "uppercase",
      letterSpacing: 0.5,
    },
    labelledField: { gap: space.xs, flexGrow: 1 },
    card: {
      backgroundColor: colors.surface,
      borderColor: colors.border,
      borderWidth: 1,
      borderRadius: radius.lg,
      padding: space.lg,
      gap: space.sm,
    },
    row: { flexDirection: "row", alignItems: "center", flexWrap: "wrap", gap: space.sm },
    field: {
      backgroundColor: colors.surface,
      borderColor: colors.borderStrong,
      borderWidth: 1,
      borderRadius: radius.sm,
      color: colors.text,
      paddingHorizontal: space.md,
      paddingVertical: space.sm,
      minWidth: 120,
      flexGrow: 1,
    },
    fieldMultiline: { minHeight: 96, textAlignVertical: "top" },
    button: {
      borderColor: colors.borderStrong,
      borderWidth: 1,
      borderRadius: radius.sm,
      paddingHorizontal: space.md + 2,
      paddingVertical: space.sm - 2,
      minHeight: 36,
      justifyContent: "center",
    },
    buttonPrimary: { backgroundColor: colors.accent, borderColor: colors.accent },
    buttonDanger: { borderColor: withAlpha(colors.danger, 0.4) },
    buttonDangerArmed: { backgroundColor: colors.danger, borderColor: colors.danger },
    buttonDim: { opacity: 0.5 },
    buttonText: { color: colors.text, fontWeight: "500" },
    buttonTextPrimary: { color: colors.onAccent, fontWeight: "600" },
    buttonTextDanger: { color: colors.danger, fontWeight: "600" },
    buttonTextDangerArmed: { color: colors.onDanger, fontWeight: "600" },
    item: {
      borderColor: colors.border,
      borderBottomWidth: 1,
      paddingVertical: space.sm,
      gap: space.xs,
    },
    spinner: { marginVertical: space.md },
    spinnerRow: { flexDirection: "row", alignItems: "center", gap: space.sm },
    badge: {
      borderRadius: radius.pill,
      borderWidth: 1,
      paddingHorizontal: space.sm,
      paddingVertical: 1,
      alignSelf: "flex-start",
    },
    badgeAccent: {
      backgroundColor: withAlpha(colors.accent, 0.1),
      borderColor: withAlpha(colors.accent, 0.45),
    },
    badgeNeutral: {
      backgroundColor: colors.surfaceRaised,
      borderColor: colors.borderStrong,
    },
    badgeTextAccent: { color: colors.accent, fontSize: 12, fontWeight: "600" },
    badgeTextNeutral: { color: colors.muted, fontSize: 12, fontWeight: "600" },
    empty: {
      alignItems: "center",
      gap: space.xs,
      paddingVertical: space.xxl,
      paddingHorizontal: space.lg,
      borderWidth: 1,
      borderStyle: "dashed",
      borderColor: colors.borderStrong,
      borderRadius: radius.lg,
    },
    emptyTitle: { color: colors.text, fontSize: 16, fontWeight: "600", letterSpacing: -0.2 },
    emptyHint: { color: colors.muted, fontSize: 13, textAlign: "center" },
    dot: { width: 10, height: 10, borderRadius: 5 },
  });
