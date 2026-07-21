/** A status "light" — the teal wordmark glow-dot, recoloured per component state. */

import type { ComponentState } from "@tenir/client-core";

const LABEL: Record<ComponentState, string> = {
  ready: "ready",
  connecting: "connecting",
  down: "down",
};

export function StatusLight({ state }: { state: ComponentState }): JSX.Element {
  return (
    <span
      className={`status-dot status-dot--${state}`}
      role="img"
      aria-label={LABEL[state]}
      title={LABEL[state]}
    />
  );
}
