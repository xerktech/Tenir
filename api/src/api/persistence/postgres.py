"""Postgres-backed conversation store.

The production ``ConversationStore``: conversations + transcript segments in
Postgres (schema in ``schema.sql``), with keyword search via Postgres
full-text search — the durable, multi-process swap for the in-memory default
behind the same Protocol. Install with ``pip install -e '.[persistence]'``.

The connection pool is opened lazily on first use so the api boots (and the
factory can select this backend) without a live database present. The SQL methods
need a running Postgres, so they are excluded from coverage; the store contract
they implement is covered by ``InMemoryConversationStore`` tests, and the schema
is exercised by the compose stack.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

from api.persistence.models import (
    Conversation,
    ConversationStatus,
    Cue,
    Segment,
    coerce_status,
    utcnow,
)

log = logging.getLogger("api.persistence.postgres")


def find_schema_file() -> Path | None:
    """Locate schema.sql. Prefer an explicit ``API_SCHEMA_PATH``; then the process
    working directory (the api image ships it at its workdir); then the repo-root
    file found by walking up from this module (dev/test). ``None`` if nowhere."""
    from api.config import settings

    candidates: list[Path] = []
    if settings.schema_path:
        candidates.append(Path(settings.schema_path))
    candidates.append(Path("schema.sql"))
    candidates.extend(parent / "schema.sql" for parent in Path(__file__).resolve().parents)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def iter_statements(sql: str) -> Iterator[str]:
    """Split a SQL script into individual statements. schema.sql has no dollar-quoted
    bodies or string-literal semicolons, so a plain ``;`` split is safe; chunks that
    are only whitespace/line-comments (e.g. the trailing newline) are skipped."""
    for chunk in sql.split(";"):
        has_sql = any(
            line.strip() and not line.strip().startswith("--") for line in chunk.splitlines()
        )
        if has_sql:
            yield chunk.strip()


def apply_schema(conn, sql: str) -> None:
    """Run every statement of an idempotent schema on ``conn``. Each is a
    ``CREATE ... IF NOT EXISTS`` / ``INSERT ... ON CONFLICT DO NOTHING``, so this is
    a no-op once the database has converged."""
    for statement in iter_statements(sql):
        conn.execute(statement)


class SqlConversationStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None

    def _ensure_pool(self):  # pragma: no cover - requires psycopg + a live database
        if self._pool is None:
            from psycopg_pool import ConnectionPool

            log.info("opening Postgres connection pool")
            self._pool = ConnectionPool(self._dsn, open=True)
            # Self-heal schema drift on boot. Postgres only applies schema.sql on a
            # FRESH data volume (docker-entrypoint-initdb.d), so a database created
            # before an additive change — e.g. the `cues` table (XERK-81) that reads
            # like get() JOIN against — never gets it, and every such read (and the
            # session.start create() that calls get()) then fails "relation does not
            # exist", killing transcription. Re-applying the idempotent schema here
            # converges an old data dir without a manual migration.
            self._apply_schema()
        return self._pool

    def _apply_schema(self) -> None:  # pragma: no cover - requires a live database
        assert self._pool is not None
        path = find_schema_file()
        if path is None:
            log.warning("schema.sql not found; skipping boot schema apply")
            return
        with self._pool.connection() as conn:
            apply_schema(conn, path.read_text(encoding="utf-8"))
        log.info("applied idempotent schema from %s on pool open", path)

    @staticmethod
    def _row_to_conversation(  # pragma: no cover
        row, segments: list[Segment], cues: list[Cue]
    ) -> Conversation:
        return Conversation(
            id=row["id"],
            household=row["household"],
            mic_source=row["mic_source"],
            source_lang=row["source_lang"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            # A database carried across an upgrade still holds statuses this build
            # no longer knows (schema.sql only runs on a fresh volume), so normalize
            # on read rather than handing a stale value up the stack (XERK-58).
            status=coerce_status(row["status"], ended=row["ended_at"] is not None),
            audio_key=row["audio_key"],
            segments=segments,
            cues=cues,
        )

    def create(  # pragma: no cover - requires a live database
        self,
        household: str,
        conversation_id: str,
        *,
        mic_source: str | None = None,
        source_lang: str | None = None,
    ) -> Conversation:
        with self._ensure_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO conversations
                    (id, household, mic_source, source_lang, started_at, status)
                VALUES (%s, %s, %s, %s, %s, 'live')
                ON CONFLICT (id) DO NOTHING
                """,
                (conversation_id, household, mic_source, source_lang, utcnow()),
            )
        got = self.get(household, conversation_id)
        assert got is not None
        return got

    def add_segment(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str, segment: Segment
    ) -> None:
        with self._ensure_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO segments
                    (segment_id, conversation_id, text, start_ms, end_ms, lang)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (segment_id) DO UPDATE SET
                    text = EXCLUDED.text, start_ms = EXCLUDED.start_ms,
                    end_ms = EXCLUDED.end_ms, lang = EXCLUDED.lang
                """,
                (
                    segment.segment_id,
                    conversation_id,
                    segment.text,
                    segment.start_ms,
                    segment.end_ms,
                    segment.lang,
                ),
            )

    def add_cue(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str, cue: Cue
    ) -> None:
        with self._ensure_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO cues
                    (cue_id, conversation_id, title, body, at_ms)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (cue_id) DO UPDATE SET
                    title = EXCLUDED.title, body = EXCLUDED.body, at_ms = EXCLUDED.at_ms
                """,
                (cue.cue_id, conversation_id, cue.title, cue.body, cue.at_ms),
            )

    def finish(  # pragma: no cover - requires a live database
        self,
        household: str,
        conversation_id: str,
        *,
        status: ConversationStatus = "ready",
    ) -> Conversation | None:
        with self._ensure_pool().connection() as conn:
            conn.execute(
                "UPDATE conversations SET ended_at = %s, status = %s "
                "WHERE id = %s AND household = %s",
                (utcnow(), status, conversation_id, household),
            )
        return self.get(household, conversation_id)

    def set_audio_key(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str, audio_key: str
    ) -> None:
        with self._ensure_pool().connection() as conn:
            conn.execute(
                "UPDATE conversations SET audio_key = %s WHERE id = %s AND household = %s",
                (audio_key, conversation_id, household),
            )

    def clear_audio_key(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str
    ) -> None:
        with self._ensure_pool().connection() as conn:
            conn.execute(
                "UPDATE conversations SET audio_key = NULL WHERE id = %s AND household = %s",
                (conversation_id, household),
            )

    def get(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str
    ) -> Conversation | None:
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            # Scope dict rows to the cursor, never the pooled connection: psycopg's
            # pool doesn't reset row_factory on return, so mutating the connection
            # leaks dict rows into the next borrower (e.g. households()'s r[0]).
            cur = conn.cursor(row_factory=dict_row)
            row = cur.execute(
                "SELECT * FROM conversations WHERE household = %s AND id = %s",
                (household, conversation_id),
            ).fetchone()
            if row is None:
                return None
            seg_rows = cur.execute(
                "SELECT * FROM segments WHERE conversation_id = %s ORDER BY start_ms",
                (conversation_id,),
            ).fetchall()
            cue_rows = cur.execute(
                "SELECT * FROM cues WHERE conversation_id = %s ORDER BY at_ms",
                (conversation_id,),
            ).fetchall()
        return self._row_to_conversation(
            row,
            [self._row_to_segment(r) for r in seg_rows],
            [self._row_to_cue(r) for r in cue_rows],
        )

    @staticmethod
    def _row_to_segment(row) -> Segment:  # pragma: no cover - requires a live database
        return Segment(
            segment_id=row["segment_id"],
            text=row["text"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            lang=row["lang"],
        )

    @staticmethod
    def _row_to_cue(row) -> Cue:  # pragma: no cover - requires a live database
        return Cue(
            cue_id=row["cue_id"],
            title=row["title"],
            body=row["body"],
            at_ms=row["at_ms"],
        )

    def list(  # pragma: no cover - requires a live database
        self, household: str, *, limit: int = 50, offset: int = 0
    ) -> list[Conversation]:
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            rows = (
                conn.cursor(row_factory=dict_row)
                .execute(
                    """
                SELECT * FROM conversations WHERE household = %s
                ORDER BY started_at DESC LIMIT %s OFFSET %s
                """,
                    (household, limit, offset),
                )
                .fetchall()
            )
        return [self.get(household, r["id"]) for r in rows]  # type: ignore[misc]

    def search(  # pragma: no cover - requires a live database
        self, household: str, query: str, *, limit: int = 50, offset: int = 0
    ) -> list[Conversation]:
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            # Match per-row so the functional FTS index on segments
            # (to_tsvector('simple', text), schema.sql) can serve the query — a
            # tsvector built over an aggregate (string_agg) can't use that index and
            # forces a full scan + per-query recompute. Rank by recency of the
            # matching conversation; relevance ranking can layer on later if needed.
            rows = (
                conn.cursor(row_factory=dict_row)
                .execute(
                    """
                SELECT c.id FROM conversations c
                WHERE c.household = %s
                  AND EXISTS (
                      SELECT 1 FROM segments s
                      WHERE s.conversation_id = c.id
                        AND to_tsvector('simple', s.text)
                            @@ websearch_to_tsquery('simple', %s)
                  )
                ORDER BY c.started_at DESC LIMIT %s OFFSET %s
                """,
                    (household, query, limit, offset),
                )
                .fetchall()
            )
        return [self.get(household, r["id"]) for r in rows]  # type: ignore[misc]

    def delete(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str
    ) -> bool:
        with self._ensure_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM conversations WHERE household = %s AND id = %s",
                (household, conversation_id),
            )
            return cur.rowcount > 0

    def households(self) -> list[str]:  # pragma: no cover - requires a live database
        with self._ensure_pool().connection() as conn:
            rows = conn.execute("SELECT DISTINCT household FROM conversations").fetchall()
        return [r[0] for r in rows]
