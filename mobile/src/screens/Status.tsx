/**
 * System status — a red/yellow/green light per backend component (master plan §status).
 *
 * Mirrors the web SPA's Status page: polls the api's GET /status snapshot so the
 * operator can confirm every component (infra, model servers, gateway) is healthy,
 * and sees a model that's still loading as yellow rather than a silent failure. A
 * transport failure means the api is unreachable — shown as the whole system down.
 */

import { Text } from "react-native";

import { useStatus } from "../lib/controllers";
import { overallStatusText, statusStateLabel } from "../lib/format";
import { Card, Heading, ListItem, Muted, Row, Screen, Spinner, StatusDot } from "../ui/components";
import { useTheme } from "../ui/ThemeContext";

export function StatusScreen(): JSX.Element {
  const { colors } = useTheme();
  const { status, unreachable, loaded } = useStatus();

  if (!loaded) {
    return (
      <Screen>
        <Heading>System status</Heading>
        <Spinner />
      </Screen>
    );
  }

  if (unreachable) {
    return (
      <Screen>
        <Heading>System status</Heading>
        <Card>
          <Row>
            <StatusDot state="down" />
            <Muted>Can&apos;t reach the server — it may be down, or the server URL may be wrong.</Muted>
          </Row>
        </Card>
      </Screen>
    );
  }

  const components = status?.components ?? [];
  return (
    <Screen>
      <Heading>System status</Heading>
      {status && (
        <Row>
          <StatusDot state={status.overall === "ready" ? "ready" : status.overall === "down" ? "down" : "connecting"} />
          <Text style={{ color: colors.text, fontWeight: "600" }}>{overallStatusText(status.overall)}</Text>
        </Row>
      )}
      {components.length === 0 ? (
        <Muted>No components are configured to monitor on this deployment.</Muted>
      ) : (
        components.map((c) => (
          <ListItem key={c.id}>
            <Row>
              <StatusDot state={c.state} />
              <Text style={{ color: colors.text }}>{c.label}</Text>
              <Muted>{statusStateLabel(c.state)}</Muted>
            </Row>
            {c.detail ? <Muted>{c.detail}</Muted> : null}
          </ListItem>
        ))
      )}
    </Screen>
  );
}
