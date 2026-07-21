import type { ReactNode } from "react";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }): JSX.Element {
  return <div className={`card ${className}`.trim()}>{children}</div>;
}
