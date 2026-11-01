/**
 * Settings card: the signed-in user's paired screen-capture devices (XERK-2594). The
 * Android twin of the web Devices tab: lists each capture PC and revokes one with the
 * two-step confirm, which disconnects it at once without logging anything else out.
 * Pairing happens on the PC itself (`tenir-capture --pair`), so there is no "add".
 */

import { devices } from "@tenir/client-core";
import { Text } from "react-native";

import { errText } from "../lib/format";
import { useNotify } from "../lib/notify";
import { useAsync } from "../lib/useAsync";
import {
  Button,
  Card,
  ConfirmButton,
  EmptyState,
  Label,
  ListItem,
  Muted,
  Row,
  Spinner,
} from "../ui/components";
import { useTheme } from "../ui/ThemeContext";

export function DevicesCard(): JSX.Element {
  const notify = useNotify();
  const { colors } = useTheme();
  const { data, loading, error, reload } = useAsync(() => devices.list());

  const revoke = (id: string, name: string) => {
    devices
      .revoke(id)
      .then(() => {
        notify(`Revoked ${name}`);
        reload();
      })
      .catch((e) => {
        notify(errText(e), "err");
        reload(); // e.g. already revoked elsewhere: don't keep showing it
      });
  };

  return (
    <Card>
      <Label>Devices</Label>
      <Muted>
        Screen-capture PCs paired to your account. Revoke one to disconnect it right away; pair
        it again with tenir-capture --pair.
      </Muted>
      {loading && <Spinner />}
      {!loading && error != null && (
        <>
          <EmptyState title="Could not load devices" hint={errText(error)} />
          <Button title="Retry" onPress={() => reload()} />
        </>
      )}
      {error == null && data?.length === 0 && (
        <EmptyState
          title="No capture devices."
          hint="Run tenir-capture --pair on the meeting PC to add one."
        />
      )}
      {data?.map((d) => (
        <ListItem key={d.id}>
          <Row>
            <Text style={{ color: colors.text, flexGrow: 1, flexShrink: 1 }}>
              {d.name} · paired {new Date(d.createdAt).toLocaleDateString()} · expires{" "}
              {new Date(d.expiresAt).toLocaleDateString()}
            </Text>
            <ConfirmButton
              title="Revoke"
              confirmTitle="Confirm revoke"
              onConfirm={() => revoke(d.id, d.name)}
            />
          </Row>
        </ListItem>
      ))}
    </Card>
  );
}
