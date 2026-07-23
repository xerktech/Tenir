/**
 * The in-app update banner (XERK-63, restyled in XERK-91) — a rounded accent
 * card under the header offering a newer Android build, mirroring the Turma
 * Android client's UpdateBanner card (`ui/UpdateBanner.kt`): leading
 * system-update icon, semibold title over a quiet subtitle, inline download
 * progress, and trailing Later/action controls. The card slides in from the
 * side when an update surfaces and slides back out on dismiss. All behaviour
 * lives in `useUpdater`; this is presentational.
 *
 * Hidden unless there's something to show, so it costs nothing on iOS / when
 * the app is current.
 */

import { useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  Animated,
  Easing,
  Pressable,
  StyleSheet,
  Text,
  useWindowDimensions,
  View,
} from "react-native";

import type { UpdateState } from "../lib/useUpdater";
import { useUpdater } from "../lib/useUpdater";
import { Button } from "./components";
import { useTheme, useThemedStyles } from "./ThemeContext";
import { radius, space, withAlpha, type Palette } from "./theme";

type VisibleState = Exclude<UpdateState, { kind: "hidden" }>;

/** Card copy per state — the same title/subtitle/action split as Turma's. */
function title(state: VisibleState): string {
  switch (state.kind) {
    case "available":
      return `Update available — v${state.version}`;
    case "downloading":
      return `Downloading v${state.version}…`;
    case "installing":
      return `Ready to install — v${state.version}`;
    case "failed":
      return "Update failed";
  }
}

function subtitle(state: VisibleState): string | null {
  switch (state.kind) {
    case "installing":
      return state.needsPermission ? "Allow Tenir to install apps, then tap Install." : null;
    case "failed":
      return `v${state.version} didn't install.`;
    default:
      return null;
  }
}

function actionLabel(state: VisibleState): string | null {
  switch (state.kind) {
    case "available":
      return "Update";
    case "installing":
      return "Install";
    case "failed":
      return "Retry";
    case "downloading":
      return null;
  }
}

export function UpdateBanner(): JSX.Element | null {
  const styles = useThemedStyles(makeStyles);
  const { colors } = useTheme();
  const { state, act, dismiss } = useUpdater();
  const { width } = useWindowDimensions();

  // Keep the last visible state mounted while the card animates away, so a
  // dismiss slides out instead of vanishing. 0 = parked offscreen right,
  // 1 = in place under the header.
  const [shown, setShown] = useState<VisibleState | null>(null);
  const slide = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    if (state.kind !== "hidden") {
      setShown(state);
      Animated.timing(slide, {
        toValue: 1,
        duration: 300,
        easing: Easing.out(Easing.cubic),
        useNativeDriver: true,
      }).start();
    } else {
      Animated.timing(slide, {
        toValue: 0,
        duration: 220,
        easing: Easing.in(Easing.cubic),
        useNativeDriver: true,
      }).start(({ finished }) => finished && setShown(null));
    }
  }, [state, slide]);

  if (shown === null) return null;

  const translateX = slide.interpolate({ inputRange: [0, 1], outputRange: [width, 0] });
  const sub = subtitle(shown);
  const action = actionLabel(shown);

  return (
    <Animated.View style={[styles.card, { transform: [{ translateX }] }]}>
      <UpdateIcon color={colors.accent} />
      <View style={styles.body}>
        <Text style={styles.title} numberOfLines={2}>
          {title(shown)}
        </Text>
        {sub !== null && <Text style={styles.subtitle}>{sub}</Text>}
        {shown.kind === "downloading" && shown.pct !== null && (
          <View style={styles.track}>
            <View style={[styles.trackFill, { width: `${shown.pct}%` }]} />
          </View>
        )}
      </View>
      {shown.kind === "downloading" ? (
        <ActivityIndicator color={colors.accent} />
      ) : (
        <View style={styles.actions}>
          {/* Only an unacted offer is dismissible — once downloaded, the only
              sensible action left is to finish installing (as in Turma). */}
          {shown.kind === "available" && (
            <Pressable accessibilityRole="button" onPress={dismiss} style={styles.later}>
              <Text style={styles.laterText}>Later</Text>
            </Pressable>
          )}
          {action !== null && <Button title={action} kind="primary" onPress={act} />}
        </View>
      )}
    </Animated.View>
  );
}

/**
 * The Material "system update" glyph — a phone outline with a download arrow —
 * drawn from plain `View`s like the tab-bar icons (no SVG dependency).
 */
function UpdateIcon({ color }: { color: string }): JSX.Element {
  return (
    <View style={{ width: 22, height: 22, alignItems: "center", justifyContent: "center" }}>
      {/* Phone outline. */}
      <View
        style={{
          width: 14,
          height: 22,
          borderRadius: 3,
          borderWidth: 2,
          borderColor: color,
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {/* Down arrow: stem into a solid triangle head. */}
        <View style={{ width: 2, height: 6, backgroundColor: color }} />
        <View
          style={{
            width: 0,
            height: 0,
            borderLeftWidth: 4,
            borderRightWidth: 4,
            borderTopWidth: 4,
            borderLeftColor: "transparent",
            borderRightColor: "transparent",
            borderTopColor: color,
          }}
        />
      </View>
    </View>
  );
}

const makeStyles = (colors: Palette) =>
  StyleSheet.create({
    // Turma's BannerCard geometry (8dp side margins, 14dp corners, 14/8/10
    // padding) with Tenir's tint formula: an accent wash fill + tinted border
    // in place of Material's solid primaryContainer.
    card: {
      flexDirection: "row",
      alignItems: "center",
      gap: space.md,
      marginHorizontal: space.sm,
      marginTop: space.sm,
      paddingLeft: space.md + 2,
      paddingRight: space.sm,
      paddingVertical: space.sm + 2,
      backgroundColor: withAlpha(colors.accent, 0.1),
      borderColor: withAlpha(colors.accent, 0.45),
      borderWidth: 1,
      borderRadius: radius.lg,
    },
    body: { flex: 1, gap: 2 },
    title: { color: colors.text, fontSize: 14, fontWeight: "600" },
    subtitle: { color: colors.muted, fontSize: 12 },
    track: {
      height: 4,
      borderRadius: radius.pill,
      backgroundColor: withAlpha(colors.accent, 0.25),
      overflow: "hidden",
      marginTop: space.xs,
    },
    trackFill: { height: 4, borderRadius: radius.pill, backgroundColor: colors.accent },
    actions: { flexDirection: "row", alignItems: "center", gap: space.xs },
    later: { paddingHorizontal: space.sm, paddingVertical: space.sm - 2, minHeight: 36, justifyContent: "center" },
    laterText: { color: colors.muted, fontWeight: "600" },
  });
