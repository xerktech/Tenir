import type { ReactNode } from "react";

export function Badge({
  children,
  tone = "accent",
}: {
  children: ReactNode;
  tone?: "accent" | "neutral";
}): JSX.Element {
  return <span className={`badge-${tone}`}>{children}</span>;
}
