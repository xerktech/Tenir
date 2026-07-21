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
import { ActivityIndicator, SafeAreaView, StyleSheet, Text, View } from "react-native";

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
import { saveServerUrl } from "./storage";
import { Button } from "./ui/components";
import { colors, space } from "./ui/theme";

const TABS = ["Live", "History", "Status", "Settings", "Privacy"] as const;
type Tab = (typeof TABS)[number];

export function App(): JSX.Element {
  const [booted, setBooted] = useState<{ wsUrl: string } | null>(null);

  useEffect(() => {
    bootstrap().then(setBooted);
  }, []);

  if (!booted) return <FullScreenSpinner />;

  return (
    <SafeAreaView style={styles.root}>
      <NotifyProvider>
        <Root initialWsUrl={booted.wsUrl} />
      </NotifyProvider>
    </SafeAreaView>
  );
}

function Root({ initialWsUrl }: { initialWsUrl: string }): JSX.Element {
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
      <View style={styles.header}>
        <Text style={styles.title}>Tenir</Text>
        <Text style={styles.identity}>
          {principal.username} · {principal.household} · {principal.role}
        </Text>
      </View>
      <Dashboard
        wsUrl={wsUrl}
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

function Dashboard({ wsUrl, settings }: { wsUrl: string; settings: JSX.Element }): JSX.Element {
  const [tab, setTab] = useState<Tab>("Live");
  return (
    <View style={styles.fill}>
      <View style={styles.tabs}>
        {TABS.map((t) => (
          <Button key={t} title={t} kind={t === tab ? "primary" : "default"} onPress={() => setTab(t)} />
        ))}
      </View>
      <View style={styles.fill}>
        {tab === "Live" && <LiveScreen wsUrl={wsUrl} />}
        {tab === "History" && <HistoryScreen />}
        {tab === "Status" && <StatusScreen />}
        {tab === "Settings" && settings}
        {tab === "Privacy" && <DisclosureScreen />}
      </View>
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
  tabs: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: space.xs,
    padding: space.sm,
  },
});
