/**
 * tenir mobile app root (master plan §8.1, §8.5).
 *
 * The phone client is a capture + browse frontend on the same api API as the web
 * SPA, built on `@tenir/client-core`. It boots by wiring a secure token store +
 * the saved server URL (`bootstrap`), then mirrors the web SPA's auth gating: auth
 * is always required, so `me()` resolves the principal when a valid token is
 * stored (straight to the dashboard) and rejects otherwise. When there's no valid
 * session, the initial **setup screen** collects the server URL plus username +
 * password together, so first-run users connect to their self-hosted api and sign
 * in from one place.
 *
 * The whole tree renders inside `ThemeProvider` — the Lumen palette (dark by
 * default, light as the counterpart) resolved from the persisted mode + OS
 * appearance, mirroring the web SPA's System/Light/Dark theming.
 */

import { useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  SafeAreaView,
  StatusBar,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { bootstrap } from "./bootstrap";
import { configureApiFromWs } from "./config";
import { useAuth } from "./lib/controllers";
import { NotifyProvider } from "./lib/notify";
import { deviceKeyValue } from "./secureStorage";
import { DisclosureScreen } from "./screens/Disclosure";
import { HistoryScreen } from "./screens/History";
import { LiveScreen } from "./screens/Live";
import { SettingsScreen } from "./screens/Settings";
import { SetupScreen } from "./screens/Setup";
import { StatusScreen } from "./screens/Status";
import { displayServerUrl, normalizeServerUrl } from "./lib/serverUrl";
import { saveLastTab, saveServerUrl } from "./storage";
import { UpdateBanner } from "./ui/UpdateBanner";
import { TabIcon } from "./ui/icons";
import { ThemeProvider, useTheme, useThemedStyles } from "./ui/ThemeContext";
import { space, type Palette } from "./ui/theme";

const TABS = ["Live", "History", "Status", "Settings", "Privacy"] as const;
type Tab = (typeof TABS)[number];

/** Narrow a persisted string back to a Tab; unknown/absent values mean Live. */
function asTab(value: string | null): Tab {
  return (TABS as readonly string[]).includes(value ?? "") ? (value as Tab) : "Live";
}

export function App(): JSX.Element {
  const kv = useMemo(() => deviceKeyValue(), []);
  const [booted, setBooted] = useState<{ wsUrl: string; lastTab: string | null } | null>(null);

  useEffect(() => {
    bootstrap().then(setBooted);
  }, []);

  return (
    <ThemeProvider kv={kv}>
      <Chrome>
        {booted ? (
          <NotifyProvider>
            <Root initialWsUrl={booted.wsUrl} initialTab={asTab(booted.lastTab)} />
          </NotifyProvider>
        ) : (
          <FullScreenSpinner />
        )}
      </Chrome>
    </ThemeProvider>
  );
}

/** Themed app chrome: paints the safe area + status bar for the active scheme. */
function Chrome({ children }: { children: React.ReactNode }): JSX.Element {
  const { colors, scheme } = useTheme();
  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }}>
      <StatusBar
        barStyle={scheme === "dark" ? "light-content" : "dark-content"}
        backgroundColor={colors.bg}
      />
      {children}
    </SafeAreaView>
  );
}

function Root({ initialWsUrl, initialTab }: { initialWsUrl: string; initialTab: Tab }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  const auth = useAuth();
  const [wsUrl, setWsUrl] = useState(initialWsUrl);

  // Persist + point client-core at a server URL (no auth side-effects).
  const applyServerUrl = (next: string) => {
    const clean = normalizeServerUrl(next);
    if (!clean) return;
    setWsUrl(clean);
    saveServerUrl(deviceKeyValue(), clean);
    configureApiFromWs(clean);
  };

  // Settings "Connect": re-point an already-signed-in user at another instance.
  const reconnect = (next: string) => {
    applyServerUrl(next);
    auth.reload();
  };

  // Initial setup: point at the chosen server, then sign in against it.
  const connectAndSignIn = async (server: string, username: string, password: string) => {
    applyServerUrl(server);
    await auth.signIn(username, password);
  };

  if (auth.loading) return <FullScreenSpinner />;

  if (!auth.data) {
    return <SetupScreen initialServerUrl={displayServerUrl(wsUrl)} onConnect={connectAndSignIn} />;
  }

  const principal = auth.data;

  return (
    <View style={styles.fill}>
      <UpdateBanner />
      <View style={styles.header}>
        {/* Wordmark row: the glow-dot + name, mirroring the web header. */}
        <View style={styles.wordmark}>
          <View style={styles.wordmarkDot} />
          <Text style={styles.title}>Tenir</Text>
        </View>
        <Text style={styles.identity}>
          {principal.username} · {principal.household} · {principal.role}
        </Text>
      </View>
      <Dashboard
        wsUrl={wsUrl}
        initialTab={initialTab}
        settings={
          <SettingsScreen
            wsUrl={displayServerUrl(wsUrl)}
            onApplyServer={reconnect}
            principal={principal}
            onSignOut={auth.signOut}
          />
        }
      />
    </View>
  );
}

function Dashboard({
  wsUrl,
  initialTab,
  settings,
}: {
  wsUrl: string;
  initialTab: Tab;
  settings: JSX.Element;
}): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  // Start on the tab the user last had open (restored at bootstrap) and
  // persist each switch, so relaunching the app keeps the user's place — the
  // mobile equivalent of the web SPA surviving a page refresh (XERK-80).
  const [tab, setTab] = useState<Tab>(initialTab);
  const selectTab = (next: Tab) => {
    setTab(next);
    saveLastTab(deviceKeyValue(), next);
  };
  return (
    <View style={styles.fill}>
      {/* Content fills the space above the fixed bottom tab bar. */}
      <View style={styles.fill}>
        {tab === "Live" && <LiveScreen wsUrl={wsUrl} />}
        {tab === "History" && <HistoryScreen />}
        {tab === "Status" && <StatusScreen />}
        {tab === "Settings" && settings}
        {tab === "Privacy" && <DisclosureScreen />}
      </View>
      <TabBar tab={tab} onSelect={selectTab} />
    </View>
  );
}

/**
 * Bottom tab bar — icon over label per page, mirroring the web SPA's mobile nav.
 * The active tab is tinted with the accent colour and carries the same 28×3 top
 * accent indicator as the web's bottom bar; the rest sit muted.
 */
function TabBar({ tab, onSelect }: { tab: Tab; onSelect: (t: Tab) => void }): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  const { colors } = useTheme();
  return (
    <View style={styles.tabBar}>
      {TABS.map((t) => {
        const active = t === tab;
        const color = active ? colors.accent : colors.muted;
        return (
          <Pressable
            key={t}
            accessibilityRole="tab"
            accessibilityState={{ selected: active }}
            accessibilityLabel={t}
            onPress={() => onSelect(t)}
            style={styles.tabItem}
          >
            {active && <View style={styles.tabActiveBar} />}
            <TabIcon name={t} color={color} />
            <Text style={[styles.tabLabel, { color }]}>{t}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

function FullScreenSpinner(): JSX.Element {
  const styles = useThemedStyles(makeStyles);
  const { colors } = useTheme();
  return (
    <View style={[styles.fill, styles.center]}>
      <ActivityIndicator color={colors.accent} />
    </View>
  );
}

const makeStyles = (colors: Palette) =>
  StyleSheet.create({
    fill: { flex: 1, backgroundColor: colors.bg },
    center: { alignItems: "center", justifyContent: "center" },
    header: {
      padding: space.md,
      gap: space.sm,
      borderBottomColor: colors.border,
      borderBottomWidth: 1,
    },
    wordmark: { flexDirection: "row", alignItems: "center", gap: space.sm },
    wordmarkDot: { width: 9, height: 9, borderRadius: 4.5, backgroundColor: colors.accent },
    title: { color: colors.text, fontSize: 18, fontWeight: "600", letterSpacing: -0.2 },
    identity: { color: colors.muted, fontSize: 12 },
    tabBar: {
      flexDirection: "row",
      borderTopColor: colors.border,
      borderTopWidth: 1,
      backgroundColor: colors.surface,
      paddingBottom: space.xs,
    },
    tabItem: {
      flex: 1,
      alignItems: "center",
      justifyContent: "center",
      gap: space.xs,
      paddingVertical: space.sm,
    },
    tabActiveBar: {
      position: "absolute",
      top: 0,
      width: 28,
      height: 3,
      borderRadius: 3,
      backgroundColor: colors.accent,
    },
    tabLabel: { fontSize: 11, fontWeight: "600" },
  });
