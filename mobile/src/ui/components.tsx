/**
 * A tiny React Native component kit so the screens read like the web SPA's markup —
 * `Screen`/`Card`/`Field`/`Button`/`ListItem` instead of raw `View`/`Text`/`Pressable`.
 * Presentational only; all behaviour lives in the container hooks (`lib/controllers`).
 */

import type { ReactNode } from "react";
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { colors, space } from "./theme";

export function Screen({ children }: { children: ReactNode }): JSX.Element {
  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.screenContent}>
      {children}
    </ScrollView>
  );
}

export function Heading({ children }: { children: ReactNode }): JSX.Element {
  return <Text style={styles.heading}>{children}</Text>;
}

export function Muted({ children }: { children: ReactNode }): JSX.Element {
  return <Text style={styles.muted}>{children}</Text>;
}

export function Card({ children }: { children: ReactNode }): JSX.Element {
  return <View style={styles.card}>{children}</View>;
}

export function Row({ children }: { children: ReactNode }): JSX.Element {
  return <View style={styles.row}>{children}</View>;
}

export function Field(props: {
  placeholder: string;
  value: string;
  onChangeText: (t: string) => void;
  secureTextEntry?: boolean;
  multiline?: boolean;
}): JSX.Element {
  return (
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
}

type ButtonKind = "default" | "primary" | "danger";

export function Button(props: {
  title: string;
  onPress: () => void;
  kind?: ButtonKind;
  disabled?: boolean;
}): JSX.Element {
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
      <Text style={[styles.buttonText, kind === "primary" && styles.buttonTextPrimary]}>
        {props.title}
      </Text>
    </Pressable>
  );
}

export function ListItem({ children }: { children: ReactNode }): JSX.Element {
  return <View style={styles.item}>{children}</View>;
}

export function Spinner(): JSX.Element {
  return <ActivityIndicator color={colors.accent} style={{ marginVertical: space.md }} />;
}

type DotState = "ready" | "connecting" | "down";

const DOT_COLOR: Record<DotState, string> = {
  ready: colors.success,
  connecting: colors.warning,
  down: colors.danger,
};

/** A status "light" — a small filled dot coloured by component state. */
export function StatusDot({ state }: { state: DotState }): JSX.Element {
  return (
    <View
      accessibilityRole="image"
      accessibilityLabel={state}
      style={[styles.dot, { backgroundColor: DOT_COLOR[state] }]}
    />
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  screenContent: { padding: space.lg, gap: space.md },
  heading: { color: colors.text, fontSize: 20, fontWeight: "700", marginBottom: space.sm },
  muted: { color: colors.muted, fontSize: 13 },
  card: {
    backgroundColor: colors.card,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 10,
    padding: space.md,
    gap: space.sm,
  },
  row: { flexDirection: "row", alignItems: "center", flexWrap: "wrap", gap: space.sm },
  field: {
    backgroundColor: colors.bg,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 8,
    color: colors.text,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    minWidth: 120,
    flexGrow: 1,
  },
  fieldMultiline: { minHeight: 96, textAlignVertical: "top" },
  button: {
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  buttonPrimary: { backgroundColor: colors.accent, borderColor: colors.accent },
  buttonDanger: { borderColor: colors.danger },
  buttonDim: { opacity: 0.5 },
  buttonText: { color: colors.text, fontWeight: "600" },
  buttonTextPrimary: { color: colors.onAccent },
  item: {
    borderColor: colors.border,
    borderBottomWidth: 1,
    paddingVertical: space.sm,
    gap: space.xs,
  },
  dot: { width: 12, height: 12, borderRadius: 6 },
});
