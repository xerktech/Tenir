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
 */

import { useEffect, useState } from "react";
import { ActivityIndicator, Pressable, SafeAreaView, StyleSheet, Text, View } from "react-native";

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
import { normalizeServerUrl } from "./lib/serverUrl";
import { saveLastTab, saveServerUrl } from "./storage";
import { UpdateBanner } from "./ui/UpdateBanner";
import { TabIcon } from "./ui/icons";
import { colors, space } from "./ui/theme";

const TABS = ["Live", "History", "Status", "Settings", "Privacy"] as const;
type Tab = (typeof TABS)[number];

/** Narrow a persisted string back to a Tab; unknown/absent values mean Live. */
function asTab(value: string | null): Tab {
  return (TABS as readonly string[]).includes(value ?? "") ? (value as Tab) : "Live";
}

export function App(): JSX.Element {
  const [booted, setBooted] = useState<{ wsUrl: string; lastTab: string | null } | null>(null);

  useEffect(() => {
    bootstrap().then(setBooted);
  }, []);

  if (!booted) return <FullScreenSpinner />;

  return (
    <SafeAreaView style={styles.root}>
      <NotifyProvider>
        <Root initialWsUrl={booted.wsUrl} initialTab={asTab(booted.lastTab)} />
      </NotifyProvider>
    </SafeAreaView>
  );
}

function Root({ initialWsUrl, initialTab }: { initialWsUrl: string; initialTab: Tab }): JSX.Element {
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
    return <SetupScreen initialServerUrl={wsUrl} onConnect={connectAndSignIn} />;
  }

  const principal = auth.data;

  return (
    <View style={styles.fill}>
      <UpdateBanner />
      <View style={styles.header}>
        <Text style={styles.title}>Tenir</Text>
        <Text style={styles.identity}>
          {principal.username} · {principal.household} · {principal.role}
        </Text>
      </View>
      <Dashboard
        wsUrl={wsUrl}
        initialTab={initialTab}
        settings={
          <SettingsScreen
            wsUrl={wsUrl}
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
 * The active tab is tinted with the accent colour; the rest sit muted.
 */
function TabBar({ tab, onSelect }: { tab: Tab; onSelect: (t: Tab) => void }): JSX.Element {
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
            <TabIcon name={t} color={color} />
            <Text style={[styles.tabLabel, { color }]}>{t}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

function FullScreenSpinner(): JSX.Element {
  return (
    <View style={[styles.root, styles.center]}>
      <ActivityIndicator color={colors.accent} />
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  fill: { flex: 1 },
  center: { alignItems: "center", justifyContent: "center" },
  header: {
    padding: space.md,
    gap: space.sm,
    borderBottomColor: colors.border,
    borderBottomWidth: 1,
  },
  title: { color: colors.text, fontSize: 18, fontWeight: "700" },
  identity: { color: colors.muted, fontSize: 12 },
  tabBar: {
    flexDirection: "row",
    borderTopColor: colors.border,
    borderTopWidth: 1,
    backgroundColor: colors.bg,
    paddingTop: space.sm,
    paddingBottom: space.sm,
  },
  tabItem: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: space.xs,
    paddingVertical: space.xs,
  },
  tabLabel: { fontSize: 11, fontWeight: "600" },
});
