/**
 * Settings: the server this app talks to, and the signed-in account.
 *
 * The server URL points the app at the user's self-hosted api (persisted and
 * re-applied on launch); "Connect" re-points an already-signed-in user at another
 * instance. The account card shows who is signed in and offers log-out.
 */

import { type Principal } from "@tenir/client-core";
import { useState } from "react";
import { Text } from "react-native";

import { useNotify } from "../lib/notify";
import { isValidServerUrl } from "../lib/serverUrl";
import { Button, Card, Field, Heading, Muted, Row, Screen } from "../ui/components";
import { colors } from "../ui/theme";

export function SettingsScreen({
  wsUrl,
  onApplyServer,
  principal,
  onSignOut,
}: {
  /** The currently configured api WebSocket URL. */
  wsUrl: string;
  /** Persist + re-point the app at a new server URL, then re-check the session. */
  onApplyServer: (url: string) => void;
  principal: Principal;
  onSignOut: () => void;
}): JSX.Element {
  const notify = useNotify();
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
        <Muted>Server</Muted>
        <Row>
          <Field placeholder="tenir.example.com" value={draft} onChangeText={setDraft} />
          <Button title="Connect" onPress={connect} />
        </Row>
      </Card>

      <Card>
        <Muted>Account</Muted>
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
