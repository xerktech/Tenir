/**
 * An inline cue: a collapsed dropdown that expands in place to reveal the body
 * (XERK-105). It replaced a click-through popup so a cue reads on the timeline
 * without a modal, defaulting to minimized so the transcript stays scannable.
 *
 * Shared by the history detail (past conversations) and the live transcript,
 * where released cues are embedded for review (XERK-108) — one component so both
 * surfaces look and behave identically.
 */

import { useId, useState } from "react";

export function CueDisclosure({
  title,
  body,
  source,
}: {
  title: string;
  body: string;
  /** Live-source attribution (XERK-120), shown under the body when present. */
  source?: string | null;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  return (
    <div className="cue-inline-block">
      <button
        className="cue-inline"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={bodyId}
        title={open ? "Hide cue detail" : "Show cue detail"}
      >
        <span className="cue-inline-caret" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
        <span className="cue-inline-mark" aria-hidden="true">
          ✦
        </span>
        <span className="cue-inline-title">{title}</span>
      </button>
      {open && (
        <div className="cue-inline-body" id={bodyId}>
          <p className="cue-inline-text">{body}</p>
          {source && <p className="cue-inline-source">{source}</p>}
        </div>
      )}
    </div>
  );
}
