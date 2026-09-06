/**
 * Initial setup / sign-in screen (master plan §8.5).
 *
 * The mobile client points at the user's *own* self-hosted api, so the very first
 * screen collects everything needed to connect in one place: the server URL plus the
 * household username + password. It normalizes + applies the server URL (persisting it
 * and pointing `client-core` at it) and then signs in — no hunting for a separate
 * server field. Returning-but-logged-out users land here too, with the server URL
 * pre-filled from their last choice.
 *
 * Optional OIDC (XERK-655): once a valid server URL is entered we ask that server
 * whether it advertises Authentik OIDC. If it does, a "Sign in with Authentik" button
 * appears **in addition to** the username/password form; if it doesn't (or the probe
 * fails), only the built-in form shows — the built-in path is unchanged either way.
 */

import { useEffect, useState } from "react";

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
  onCheckOidc,
  onOidcConnect,
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
  /**
   * Ask whether the given server advertises OIDC (resolves its provider as a side
   * effect). Drives whether the Authentik button is shown; absent ⇒ never shown.
   */
  onCheckOidc?: (serverUrl: string) => Promise<boolean>;
  /** Start the native OIDC login against the given server; rejects on failure/cancel. */
  onOidcConnect?: (serverUrl: string) => Promise<void>;
}): JSX.Element {
  const notify = useNotify();
  const [serverUrl, setServerUrl] = useState(initialServerUrl);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [oidcAvailable, setOidcAvailable] = useState(false);
  const [oidcBusy, setOidcBusy] = useState(false);

  const ready =
    !busy && !oidcBusy && isValidServerUrl(serverUrl) && username.trim() !== "" && password !== "";

  // Probe the entered server for OIDC once the URL looks valid, debounced so we don't
  // hit a new address on every keystroke. Any change resets the button until the probe
  // for the *current* URL confirms it, and a stale in-flight probe is ignored.
  useEffect(() => {
    if (!onCheckOidc || !isValidServerUrl(serverUrl)) {
      setOidcAvailable(false);
      return;
    }
    let cancelled = false;
    setOidcAvailable(false);
    const id = setTimeout(() => {
      void onCheckOidc(normalizeServerUrl(serverUrl))
        .then((available) => {
          if (!cancelled) setOidcAvailable(available);
        })
        .catch(() => {
          if (!cancelled) setOidcAvailable(false);
        });
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, [serverUrl, onCheckOidc]);

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

  const oidcSubmit = async () => {
    if (!onOidcConnect || !isValidServerUrl(serverUrl)) return;
    setOidcBusy(true);
    try {
      await onOidcConnect(normalizeServerUrl(serverUrl));
      notify("Connected");
    } catch (e) {
      notify(errText(e), "err");
    } finally {
      setOidcBusy(false);
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
        {/* Only shown when the server reports OIDC available (XERK-655). Opens Authentik
            in the system browser and returns via the app's custom scheme. */}
        {oidcAvailable && (
          <>
            <Button
              title={oidcBusy ? "Opening Authentik…" : "Sign in with Authentik"}
              kind="primary"
              disabled={busy || oidcBusy}
              onPress={() => void oidcSubmit()}
            />
            <Muted>or sign in with your username &amp; password</Muted>
          </>
        )}
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
          kind={oidcAvailable ? "default" : "primary"}
          disabled={!ready}
          onPress={() => void submit()}
        />
      </Card>
      <Muted>{DISCLOSURE_SUMMARY}</Muted>
    </Screen>
  );
}
