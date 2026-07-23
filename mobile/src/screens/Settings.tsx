/**
 * Settings: the server this app talks to, the app's appearance, and the
 * signed-in account.
 *
 * The server URL points the app at the user's self-hosted api (persisted and
 * re-applied on launch); "Connect" re-points an already-signed-in user at another
 * instance. Appearance offers the same System/Light/Dark choice as the web SPA's
 * header toggle (persisted under the same `tenir.theme` key). The account card
 * shows who is signed in and offers log-out.
 */

import { type Principal } from "@tenir/client-core";
import { useState } from "react";
import { Text } from "react-native";

import { useNotify } from "../lib/notify";
import { isValidServerUrl } from "../lib/serverUrl";
import { Button, Card, Field, Heading, Label, Row, Screen } from "../ui/components";
import { useTheme } from "../ui/ThemeContext";
import type { ThemeMode } from "../ui/theme";

const THEME_MODES: { mode: ThemeMode; label: string }[] = [
  { mode: "system", label: "System" },
  { mode: "light", label: "Light" },
  { mode: "dark", label: "Dark" },
];

export function SettingsScreen({
  wsUrl,
  onApplyServer,
  principal,
  onSignOut,
}: {
  /** The configured server address in friendly display form (host, no `wss://`/`/ws`). */
  wsUrl: string;
  /** Persist + re-point the app at a new server URL, then re-check the session. */
  onApplyServer: (url: string) => void;
  principal: Principal;
  onSignOut: () => void;
}): JSX.Element {
  const notify = useNotify();
  const { colors, mode, setMode } = useTheme();
  const [draft, setDraft] = useState(wsUrl);

  const connect = () => {
    if (!isValidServerUrl(draft)) {
      notify("Enter your server address, e.g. tenir.example.com", "err");
      return;
    }
    onApplyServer(draft);
    notify("Server updated");
  };

  return (
    <Screen>
      <Heading>Settings</Heading>

      <Card>
        <Label>Server</Label>
        <Row>
          <Field placeholder="tenir.example.com" value={draft} onChangeText={setDraft} />
          <Button title="Connect" onPress={connect} />
        </Row>
      </Card>

      <Card>
        <Label>Appearance</Label>
        <Row>
          {THEME_MODES.map((t) => (
            <Button
              key={t.mode}
              title={t.label}
              kind={mode === t.mode ? "primary" : "default"}
              onPress={() => setMode(t.mode)}
            />
          ))}
        </Row>
      </Card>

      <Card>
        <Label>Account</Label>
        <Row>
          <Text style={{ color: colors.text, flexGrow: 1 }}>
            {principal.username} · {principal.household} · {principal.role}
          </Text>
          <Button title="Log out" kind="danger" onPress={onSignOut} />
        </Row>
      </Card>
    </Screen>
  );
}
