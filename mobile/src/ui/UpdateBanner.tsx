/**
 * The in-app update banner (XERK-63) — a dismissible strip at the top of the
 * dashboard offering a newer Android build, mirroring the Turma client's
 * UpdateBanner. All behaviour lives in `useUpdater`; this is presentational.
 *
 * Hidden unless there's something to show, so it costs nothing on iOS / when the
 * app is current.
 */

import { StyleSheet, Text, View } from "react-native";

import { useUpdater } from "../lib/useUpdater";
import { Button } from "./components";
import { colors, space } from "./theme";

export function UpdateBanner(): JSX.Element | null {
  const { state, act, dismiss } = useUpdater();

  if (state.kind === "hidden") return null;

  let message: string;
  let action: string | null = "Update";
  switch (state.kind) {
    case "available":
      message = `Update available — v${state.version}`;
      break;
    case "downloading":
      message = state.pct === null ? "Downloading…" : `Downloading… ${state.pct}%`;
      action = null;
      break;
    case "installing":
      message = state.needsPermission
        ? "Allow installs from Tenir, then tap Install"
        : `Ready to install v${state.version}`;
      action = state.needsPermission ? "Install" : "Reinstall";
      break;
    case "failed":
      message = `Update to v${state.version} failed`;
      action = "Retry";
      break;
  }

  return (
    <View style={styles.banner}>
      <Text style={styles.text} numberOfLines={2}>
        {message}
      </Text>
      <View style={styles.actions}>
        {action !== null && <Button title={action} kind="primary" onPress={act} />}
        <Button title="Dismiss" onPress={dismiss} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    flexWrap: "wrap",
    gap: space.sm,
    padding: space.sm,
    backgroundColor: colors.card,
    borderBottomColor: colors.accent,
    borderBottomWidth: 2,
  },
  text: { color: colors.text, fontSize: 13, flexShrink: 1 },
  actions: { flexDirection: "row", gap: space.xs },
});
