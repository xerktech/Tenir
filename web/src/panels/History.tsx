/** Conversation history & search over stored speech-to-text sessions. */

import {
  history,
  meetingSpeakers,
  platformLabel,
  type Conversation,
  type ConversationSummary,
  type CueView,
  type SegmentView,
  type SongView,
} from "@tenir/client-core";
import { useMemo, useState } from "react";

import { useAsync } from "../lib/hooks";
import { errText, useNotify } from "../lib/toast";
import {
  Badge,
  Button,
  Card,
  ConfirmButton,
  CueDisclosure,
  EmptyState,
  Input,
  LangTag,
  Spinner,
  TranslationLine,
} from "../ui";

type SortKey = "date" | "duration" | "turns" | "status";
type SortDir = "asc" | "desc";

interface Column {
  key: SortKey;
  label: string;
  className?: string;
}

const COLUMNS: Column[] = [
  { key: "date", label: "Date", className: "history-col-date" },
  { key: "duration", label: "Duration" },
  { key: "turns", label: "Turns", className: "history-col-turns" },
  { key: "status", label: "Status", className: "history-col-status" },
];

function compare(a: ConversationSummary, b: ConversationSummary, key: SortKey): number {
  switch (key) {
    case "date":
      return new Date(a.startedAt).getTime() - new Date(b.startedAt).getTime();
    case "duration":
      return a.durationMs - b.durationMs;
    case "turns":
      return a.segmentCount - b.segmentCount;
    case "status":
      return a.status.localeCompare(b.status);
  }
}

/** Render a millisecond span as m:ss (hours folded into minutes). */
export function formatDuration(ms: number): string {
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/** Org badge + platform of a screen-captured meeting (XERK-2507); nothing for a mic session. */
function MeetingTags({ conv }: { conv: ConversationSummary }): JSX.Element | null {
  if (conv.source !== "screen") return null;
  return (
    <span className="meeting-tags">
      {conv.orgName && <Badge>{conv.orgName}</Badge>}
      {conv.platform && <span className="muted">{platformLabel(conv.platform)}</span>}
    </span>
  );
}

export function HistoryPanel(): JSX.Element {
  const notify = useNotify();
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  // "" = every org. The filter only shows once there are meetings to filter (XERK-2507).
  const [org, setOrg] = useState("");
  const { data, error, loading, reload } = useAsync(
    () => history.list(search.trim() || undefined, 50, 0, org || undefined),
    [search, org],
  );
  // A failed org list just hides the filter: the history itself still loads.
  const orgs = useAsync(() => history.orgs().catch(() => []), []);
  const [selected, setSelected] = useState<Conversation | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("date");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const rows = useMemo(() => {
    if (!data) return data;
    const dir = sortDir === "asc" ? 1 : -1;
    return [...data].sort((a, b) => compare(a, b, sortKey) * dir);
  }, [data, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // Dates feel most natural newest-first; everything else reads best ascending.
      setSortDir(key === "date" ? "desc" : "asc");
    }
  };

  const open = (id: string) =>
    history
      .get(id)
      .then(setSelected)
      .catch((e) => notify(errText(e), "err"));

  const remove = (id: string) =>
    history
      .remove(id)
      .then(() => {
        if (selected?.id === id) setSelected(null);
        notify("Conversation deleted");
        reload();
      })
      // A failed delete used to leave the row sitting there for good — the row
      // is only ever removed by a successful reload, so a 404 (already deleted
      // elsewhere) left a phantom that could never be cleared. Reload either
      // way (XERK-236).
      .catch((e) => {
        notify(errText(e), "err");
        reload();
      });

  // Opening a conversation shows its transcript on its own page, replacing the
  // list. It used to render inline at the bottom of the list, below the fold,
  // where it read as the click doing nothing (XERK-65).
  if (selected) {
    return (
      <ConversationDetail
        conv={selected}
        onDelete={() => void remove(selected.id)}
        onBack={() => setSelected(null)}
      />
    );
  }

  return (
    <section>
      <h2>History &amp; search</h2>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          setSearch(query);
        }}
      >
        <Input
          className="grow"
          placeholder="Search what you've heard…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <Button variant="primary" type="submit">
          Search
        </Button>
        {(orgs.data?.length ?? 0) > 0 && (
          <select
            aria-label="Filter by org"
            className="input history-org-filter"
            value={org}
            onChange={(e) => setOrg(e.target.value)}
          >
            <option value="">All orgs</option>
            {orgs.data?.map((o) => (
              <option key={o.siteKey} value={o.siteKey}>
                {o.name}
              </option>
            ))}
          </select>
        )}
      </form>

      {loading && <Spinner />}
      {/* A failed listing used to render as an empty section, indistinguishable from
          having recorded nothing (XERK-58). Say so, and offer a retry. */}
      {!loading && error != null && (
        <div>
          <EmptyState title="Could not load history" hint={errText(error)} />
          <Button onClick={reload}>Retry</Button>
        </div>
      )}
      {error == null && data?.length === 0 && (
        <EmptyState title="No conversations yet" hint="Captured conversations will appear here." />
      )}

      {rows && rows.length > 0 && (
        <table className="history-table">
          <thead>
            <tr>
              {COLUMNS.map((col) => (
                <th
                  key={col.key}
                  className={col.className}
                  aria-sort={sortKey === col.key ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
                >
                  <button className="history-sort" onClick={() => toggleSort(col.key)} type="button">
                    {col.label}
                    <span className="history-sort-caret" aria-hidden="true">
                      {sortKey === col.key ? (sortDir === "asc" ? "▲" : "▼") : ""}
                    </span>
                  </button>
                </th>
              ))}
              <th className="history-col-actions">
                <span className="history-sort">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id} className="history-row">
                <td className="history-col-date">
                  <button className="link" onClick={() => void open(c.id)}>
                    {new Date(c.startedAt).toLocaleString()}
                  </button>
                  {c.source === "screen" && (
                    <div className="history-meeting">
                      <span className="history-meeting-title">{c.meetingTitle || "Meeting"}</span>
                      <MeetingTags conv={c} />
                    </div>
                  )}
                </td>
                <td>{formatDuration(c.durationMs)}</td>
                <td className="history-col-turns">{c.segmentCount}</td>
                <td className="history-col-status">{c.status}</td>
                <td className="history-col-actions">
                  {/* Destructive actions arm on the first click and commit on the
                      second (Turma's two-step pattern) — no confirm dialog.
                      A conversation that is still LIVE is the one being recorded
                      right now: deleting it took the row out from under the
                      running session, so every word spoken afterwards was
                      silently discarded and the session could not be saved
                      (XERK-236). Stop it first. */}
                  <ConfirmButton
                    confirmLabel="Confirm delete"
                    disabled={c.status === "live"}
                    title={c.status === "live" ? "Stop the recording before deleting it" : undefined}
                    onConfirm={() => void remove(c.id)}
                  >
                    Delete
                  </ConfirmButton>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** Segment timing rendered as "m:ss–m:ss" offsets from the session start. */
function segmentTiming(s: SegmentView): string {
  return `${formatDuration(s.startMs)}–${formatDuration(s.endMs)}`;
}

// A transcript row is a spoken segment, a private cue, or a recognized song,
// placed on the same timeline (XERK-81, XERK-184). Cues/songs carry `atMs`;
// segments carry `startMs`.
type TranscriptItem =
  | { kind: "segment"; at: number; seg: SegmentView }
  | { kind: "cue"; at: number; cue: CueView }
  | { kind: "song"; at: number; song: SongView };

/** Merge segments, cues, and songs into one timeline, ordered by position (a cue
 *  or song sits after the segment it shares a timestamp with). */
function timeline(conv: Conversation): TranscriptItem[] {
  const items: TranscriptItem[] = [
    ...conv.segments.map((seg) => ({ kind: "segment" as const, at: seg.startMs, seg })),
    // Tolerate an older/partial payload without cues/songs rather than crashing the detail.
    ...(conv.cues ?? []).map((cue) => ({ kind: "cue" as const, at: cue.atMs, cue })),
    ...(conv.songs ?? []).map((song) => ({ kind: "song" as const, at: song.atMs, song })),
  ];
  // Stable-ish: order by position, breaking ties with segment before any aside so
  // a cue/song reads as landing just after the words around it.
  return items.sort((a, b) => a.at - b.at || (a.kind === "segment" ? 0 : 1) - (b.kind === "segment" ? 0 : 1));
}

/** Everyone heard or seen in a meeting (XERK-2507). Read-only in v1: renaming speakers
 *  (aliases) waits on XERK-2606. */
function SpeakerList({ conv }: { conv: Conversation }): JSX.Element | null {
  const speakers = meetingSpeakers(conv);
  if (speakers.length === 0) return null;
  return <p className="muted speaker-list">Speakers: {speakers.join(", ")}</p>;
}

function ConversationDetail({
  conv,
  onDelete,
  onBack,
}: {
  conv: Conversation;
  onDelete: () => void;
  onBack: () => void;
}): JSX.Element {
  // Cues default to shown every time a conversation is opened (not persisted).
  // Hiding them drops the inline cue blocks so the transcript reads as one
  // uninterrupted, fully-selectable run — mirroring the mobile toggle (XERK-104).
  const [showCues, setShowCues] = useState(true);
  const hasCues = (conv.cues?.length ?? 0) > 0;
  const items = timeline(conv).filter((item) => showCues || item.kind === "segment");
  return (
    <Card className="detail">
      <div className="row">
        <Button variant="ghost" onClick={onBack}>
          ← History
        </Button>
        <h3 className="grow">
          {conv.source === "screen" ? conv.meetingTitle || "Meeting" : "Conversation detail"}
        </h3>
        <ConfirmButton confirmLabel="Confirm delete" onConfirm={onDelete}>
          Delete
        </ConfirmButton>
      </div>
      <p className="muted">
        {new Date(conv.startedAt).toLocaleString()} · {formatDuration(conv.durationMs)} · {conv.segmentCount} turns
      </p>
      <MeetingTags conv={conv} />
      {conv.source === "screen" && <SpeakerList conv={conv} />}
      {/* Only shown when there are cues to toggle. */}
      {hasCues && (
        <label className="cue-toggle">
          <input
            type="checkbox"
            checked={showCues}
            onChange={(e) => setShowCues(e.target.checked)}
          />
          Show cues
        </label>
      )}
      <div className="transcript-block">
        {/* A session can hold no turns at all (nothing was said, or the transcript
            was lost). Say so — an empty block reads as a detail that failed to open. */}
        {conv.segments.length === 0 &&
        (conv.cues?.length ?? 0) === 0 &&
        (conv.songs?.length ?? 0) === 0 ? (
          <p className="muted">No transcript was recorded for this session.</p>
        ) : (
          items.map((item) =>
            item.kind === "segment" ? (
              <div className="item" key={item.seg.segmentId}>
                <span className="muted">{segmentTiming(item.seg)}</span>{" "}
                {/* Who said it, on a screen-captured meeting (XERK-2507). */}
                {item.seg.speaker && <span className="speaker-name">{item.seg.speaker}: </span>}
                {/* Source-language chip on a translated turn's original text
                    (XERK-160) — the counterpart of the translation's "EN" tag. */}
                {item.seg.translation && item.seg.lang && <LangTag lang={item.seg.lang} />}
                {item.seg.text}
                {/* Stored English translation of a non-English turn (XERK-160). */}
                {item.seg.translation && (
                  <TranslationLine text={item.seg.translation} lang={item.seg.lang} />
                )}
              </div>
            ) : item.kind === "cue" ? (
              <CueDisclosure
                key={item.cue.cueId}
                title={item.cue.title}
                body={item.cue.body}
                source={item.cue.source}
              />
            ) : (
              // A song recognized playing during the session (XERK-184).
              <div className="item song-item" key={item.song.songId}>
                <span className="song-badge" aria-hidden="true">
                  ♪
                </span>{" "}
                <span className="song-title">
                  {item.song.title} — {item.song.artist}
                </span>
              </div>
            ),
          )
        )}
      </div>
      {conv.hasAudio && (
        <div className="audio-player">
          {/* Native playback with the browser's built-in transport + seek bar
              (XERK-67). The api serves the clip inline with byte-range support so
              the scrubber can seek. The nested link is the no-<audio> fallback and
              keeps the clip downloadable. */}
          <audio className="audio-player-el" controls preload="metadata" src={history.audioUrl(conv.id)}>
            <a href={history.audioUrl(conv.id)} download>
              audio.wav
            </a>
          </audio>
          <a className="link" href={history.audioUrl(conv.id)} download>
            Download audio.wav
          </a>
        </div>
      )}
    </Card>
  );
}
