/** Conversation history & search over stored speech-to-text sessions. */

import { history, type Conversation, type ConversationSummary, type SegmentView } from "@tenir/client-core";
import { useMemo, useState } from "react";

import { useAsync } from "../lib/hooks";
import { errText, useNotify } from "../lib/toast";
import { Button, Card, EmptyState, Input, Spinner } from "../ui";

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

export function HistoryPanel(): JSX.Element {
  const notify = useNotify();
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const { data, error, loading, reload } = useAsync(() => history.list(search.trim() || undefined), [search]);
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
        reload();
      })
      .catch((e) => notify(errText(e), "err"));

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
                </td>
                <td>{formatDuration(c.durationMs)}</td>
                <td className="history-col-turns">{c.segmentCount}</td>
                <td className="history-col-status">{c.status}</td>
                <td className="history-col-actions">
                  <Button variant="danger" onClick={() => void remove(c.id)}>
                    Delete
                  </Button>
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

function ConversationDetail({
  conv,
  onDelete,
  onBack,
}: {
  conv: Conversation;
  onDelete: () => void;
  onBack: () => void;
}): JSX.Element {
  return (
    <Card className="detail">
      <div className="row">
        <Button variant="ghost" onClick={onBack}>
          ← History
        </Button>
        <h3 className="grow">Conversation detail</h3>
        <Button variant="danger" onClick={onDelete}>
          Delete
        </Button>
      </div>
      <p className="muted">
        {new Date(conv.startedAt).toLocaleString()} · {formatDuration(conv.durationMs)} · {conv.segmentCount} turns
      </p>
      <div className="transcript-block">
        {/* A session can hold no turns at all (nothing was said, or the transcript
            was lost). Say so — an empty block reads as a detail that failed to open. */}
        {conv.segments.length === 0 ? (
          <p className="muted">No transcript was recorded for this session.</p>
        ) : (
          conv.segments.map((s) => (
            <div className="item" key={s.segmentId}>
              <span className="muted">{segmentTiming(s)}</span> {s.text}
            </div>
          ))
        )}
      </div>
      {conv.hasAudio && (
        <p>
          <a href={history.audioUrl(conv.id)} download>
            audio.wav
          </a>
        </p>
      )}
    </Card>
  );
}
