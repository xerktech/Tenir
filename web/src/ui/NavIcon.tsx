/**
 * Line icons for the dashboard page navigation — one glyph per section (Live,
 * History, Status, Users). They render as `currentColor` strokes so they inherit
 * the nav item's muted/active colour, and are marked `aria-hidden` so the button's
 * accessible name stays the text label (the icon is decorative reinforcement).
 */

import type { JSX } from "react";

/** The dashboard sections that carry a nav icon. */
export type NavPage = "Live" | "History" | "Status" | "Users";

type IconProps = { size?: number };

const svgProps = (size: number) =>
  ({
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
    focusable: false,
  }) as const;

/** Microphone — live capture. */
function LiveIcon({ size = 18 }: IconProps): JSX.Element {
  return (
    <svg {...svgProps(size)}>
      <rect x="9" y="2" width="6" height="11" rx="3" />
      <path d="M5 10a7 7 0 0 0 14 0" />
      <line x1="12" y1="19" x2="12" y2="22" />
    </svg>
  );
}

/** Clock — recorded, browsable history. */
function HistoryIcon({ size = 18 }: IconProps): JSX.Element {
  return (
    <svg {...svgProps(size)}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}

/** Ascending bars — system health / status (matches the mobile tab icon). */
function StatusIcon({ size = 18 }: IconProps): JSX.Element {
  return (
    <svg {...svgProps(size)}>
      <line x1="5" y1="20" x2="5" y2="14" />
      <line x1="12" y1="20" x2="12" y2="9" />
      <line x1="19" y1="20" x2="19" y2="4" />
    </svg>
  );
}

/** Two people — household user management. */
function UsersIcon({ size = 18 }: IconProps): JSX.Element {
  return (
    <svg {...svgProps(size)}>
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}

const ICONS: Record<NavPage, (p: IconProps) => JSX.Element> = {
  Live: LiveIcon,
  History: HistoryIcon,
  Status: StatusIcon,
  Users: UsersIcon,
};

/** Render the icon for a dashboard page. */
export function NavIcon({ page, size }: { page: NavPage; size?: number }): JSX.Element {
  const Icon = ICONS[page];
  return <Icon size={size} />;
}
