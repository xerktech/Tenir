/** In-app biometric/recording disclosures (master plan §9, risk #6) — also the copy
 *  surfaced to App Store / Play review before submission. */

import { Text } from "react-native";

import { DISCLOSURES } from "@tenir/client-core";
import { Card, Heading, Muted, Screen } from "../ui/components";
import { colors } from "../ui/theme";

export function DisclosureScreen(): JSX.Element {
  return (
    <Screen>
      <Heading>Privacy &amp; recording</Heading>
      {DISCLOSURES.map((d) => (
        <Card key={d.id}>
          <Text style={{ color: colors.text, fontWeight: "700" }}>{d.title}</Text>
          <Muted>{d.body}</Muted>
        </Card>
      ))}
    </Screen>
  );
}
