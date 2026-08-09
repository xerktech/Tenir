/**
 * Initial setup / sign-in screen (master plan §8.5).
 *
 * The mobile client points at the user's *own* self-hosted api, so the very first
 * screen collects everything needed to connect in one place: the server URL plus the
 * household username + password. It normalizes + applies the server URL (persisting it
 * and pointing `client-core` at it) and then signs in — no hunting for a separate
 * server field. Returning-but-logged-out users land here too, with the server URL
 * pre-filled from their last choice.
 */

import { useState } from "react";

import { DISCLOSURE_SUMMARY } from "@tenir/client-core";
import { errText } from "../lib/format";
import { useNotify } from "../lib/notify";
import { isValidServerUrl, normalizeServerUrl } from "../lib/serverUrl";
import { Button, Card, Field, Heading, Muted, Screen } from "../ui/components";

export function SetupScreen({
  initialServerUrl,
  onConnect,
  unreachable = false,
  onRetry,
}: {
  /** Server URL to pre-fill (the persisted choice, or the default seed). */
  initialServerUrl: string;
  /** Apply the normalized server URL, then sign in; rejects on a bad URL/credentials. */
  onConnect: (serverUrl: string, username: string, password: string) => Promise<void>;
  /** The stored session could not be checked because the server is unreachable
   *  — the user may well still be signed in (XERK-236). */
  unreachable?: boolean;
  /** Re-check the stored session. */
  onRetry?: () => void;
}): JSX.Element {
  const notify = useNotify();
  const [serverUrl, setServerUrl] = useState(initialServerUrl);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const ready =
    !busy && isValidServerUrl(serverUrl) && username.trim() !== "" && password !== "";

  const submit = async () => {
    if (!isValidServerUrl(serverUrl)) {
      notify("Enter your server address, e.g. tenir.example.com", "err");
      return;
    }
    setBusy(true);
    try {
      await onConnect(normalizeServerUrl(serverUrl), username.trim(), password);
      notify("Connected");
    } catch (e) {
      notify(errText(e), "err");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Screen>
      <Heading>{unreachable ? "Can't reach your server" : "Set up Tenir"}</Heading>
      {unreachable && (
        <Card>
          <Muted>
            Your Tenir server didn&apos;t answer, so we couldn&apos;t check whether you&apos;re
            still signed in. If it&apos;s just offline, you don&apos;t need to sign in again —
            start it and retry.
          </Muted>
          {onRetry && <Button title="Retry" onPress={onRetry} />}
        </Card>
      )}
      <Card>
        <Muted>Enter your Tenir server address, then sign in.</Muted>
        <Field label="Server" placeholder="tenir.example.com" value={serverUrl} onChangeText={setServerUrl} />
        <Field label="Username" placeholder="username" value={username} onChangeText={setUsername} />
        <Field
          label="Password"
          placeholder="password"
          value={password}
          onChangeText={setPassword}
          secureTextEntry
        />
        <Button
          title={busy ? "Connecting…" : "Connect & sign in"}
          kind="primary"
          disabled={!ready}
          onPress={() => void submit()}
        />
      </Card>
      <Muted>{DISCLOSURE_SUMMARY}</Muted>
    </Screen>
  );
}
