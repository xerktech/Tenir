/**
 * Monochrome tab-bar icons drawn from plain React Native `View`s — no SVG/native
 * dependency, matching the repo's vector-only, no-binary-assets approach (see the
 * Android launcher `ic_launcher.xml`). Each icon fills a fixed square box and is
 * tinted via the `color` prop so the bottom tab bar can colour it accent-when-active
 * / muted-when-idle, mirroring the web SPA's page nav.
 */

import type { ReactNode } from "react";
import { View } from "react-native";

/** The mobile dashboard sections that carry a tab icon. */
export type TabIconName = "Live" | "History" | "Status" | "Settings" | "Privacy";

type IconProps = { color: string; size?: number };

const BOX = 22;

function Box({ size = BOX, children }: { size?: number; children: ReactNode }): JSX.Element {
  return (
    <View style={{ width: size, height: size, alignItems: "center", justifyContent: "center" }}>
      {children}
    </View>
  );
}

/** Record ring + dot — live capture. */
function LiveIcon({ color, size }: IconProps): JSX.Element {
  return (
    <Box size={size}>
      <View
        style={{
          width: 18,
          height: 18,
          borderRadius: 9,
          borderWidth: 2,
          borderColor: color,
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: color }} />
      </View>
    </Box>
  );
}

/** Stacked rows — the recorded session list / history. */
function HistoryIcon({ color, size }: IconProps): JSX.Element {
  const bar = { height: 2.5, borderRadius: 2, backgroundColor: color } as const;
  return (
    <Box size={size}>
      <View style={{ width: 18, gap: 4 }}>
        <View style={[bar, { width: 18 }]} />
        <View style={[bar, { width: 14 }]} />
        <View style={[bar, { width: 18 }]} />
      </View>
    </Box>
  );
}

/** Ascending bars — system health / status. */
function StatusIcon({ color, size }: IconProps): JSX.Element {
  const bar = { width: 4, borderRadius: 2, backgroundColor: color } as const;
  return (
    <Box size={size}>
      <View style={{ flexDirection: "row", alignItems: "flex-end", gap: 3, height: 18 }}>
        <View style={[bar, { height: 8 }]} />
        <View style={[bar, { height: 13 }]} />
        <View style={[bar, { height: 18 }]} />
      </View>
    </Box>
  );
}

/** Sliders — settings. */
function SettingsIcon({ color, size }: IconProps): JSX.Element {
  const track = { height: 2, borderRadius: 2, backgroundColor: color } as const;
  const knob = {
    position: "absolute" as const,
    top: -3,
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: color,
  };
  return (
    <Box size={size}>
      <View style={{ width: 18, gap: 7 }}>
        <View>
          <View style={[track, { width: 18 }]} />
          <View style={[knob, { left: 3 }]} />
        </View>
        <View>
          <View style={[track, { width: 18 }]} />
          <View style={[knob, { right: 3 }]} />
        </View>
      </View>
    </Box>
  );
}

/** Padlock — the privacy / disclosure page. */
function PrivacyIcon({ color, size }: IconProps): JSX.Element {
  return (
    <Box size={size}>
      <View style={{ alignItems: "center" }}>
        {/* Shackle: top half of a rounded rect (no bottom edge). */}
        <View
          style={{
            width: 10,
            height: 8,
            borderColor: color,
            borderWidth: 2,
            borderBottomWidth: 0,
            borderTopLeftRadius: 5,
            borderTopRightRadius: 5,
            marginBottom: -1,
          }}
        />
        {/* Body. */}
        <View
          style={{
            width: 16,
            height: 11,
            borderRadius: 3,
            backgroundColor: color,
          }}
        />
      </View>
    </Box>
  );
}

const ICONS: Record<TabIconName, (p: IconProps) => JSX.Element> = {
  Live: LiveIcon,
  History: HistoryIcon,
  Status: StatusIcon,
  Settings: SettingsIcon,
  Privacy: PrivacyIcon,
};

/** Render the bottom-tab icon for a mobile dashboard section. */
export function TabIcon({ name, color, size }: { name: TabIconName; color: string; size?: number }): JSX.Element {
  const Icon = ICONS[name];
  return <Icon color={color} size={size} />;
}
