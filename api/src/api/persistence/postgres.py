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

import contextlib
import functools
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from api.persistence.models import (
    Conversation,
    ConversationStatus,
    ConversationSummary,
    Cue,
    Segment,
    Song,
    coerce_status,
    utcnow,
)

log = logging.getLogger("api.persistence.postgres")

# How long opening the pool may wait for its first connections, how long a failed
# open is remembered before the next attempt, and how long any caller waits for a
# pooled connection. psycopg's 30s default applied to every call while the database was down,
# each one building a fresh pool behind ``_pool_lock``: boot blocked ~2 minutes and
# every /ready held a worker thread 30-60s (XERK-1434).
OPEN_TIMEOUT_SECONDS = 5.0

# Client-side bounds for a database that stops answering after the pool opened
# (XERK-1513). A hung-but-ACKing server (SIGSTOP, a wedged host) never errors, so
# neither connect_timeout nor a server-side statement_timeout ends the wait: the
# pool's connection check and every borrow held a worker thread until TCP gave up.
# A watchdog shuts the connection's socket down instead, failing the blocked call.
# The check is an empty query, so its bound can be tight; a borrow spans every
# statement a request runs on its connection.
CHECK_TIMEOUT_SECONDS = 2.0
QUERY_TIMEOUT_SECONDS = 15.0

# Server-side bound on every request-path statement, below QUERY_TIMEOUT_SECONDS so
# a live but slow or lock-blocked database ends the statement itself. Severing only
# the client side left the backend waiting: with steady traffic through a lock wait
# each severed borrow orphaned one backend and the pool opened a replacement, so the
# connection count grew by ~4 per pool every 15s (XERK-1513 QA). The boot schema
# apply lifts it for its own transaction. Its 57014 is deliberately not an outage:
# the stale sweep and the admin seed would retry a statement that is slow every time
# forever (see _reconcile_is_retryable). Only the session's held writes retry it
# (``is_retryable_write``), since dropping them loses the recording.
STATEMENT_TIMEOUT_SECONDS = 10.0

# How long a request waits for a connection once the pool has given up reconnecting
# (XERK-1513). Every request waited the full OPEN_TIMEOUT_SECONDS during an outage,
# so a burst queued behind the 40-thread worker limiter for ceil(N/40) x 5s and
# stalled unrelated endpoints. The short wait still lets a request kick off the
# pool's next connect attempt, so the first one after the database is back
# succeeds and closes the breaker.
OUTAGE_WAIT_SECONDS = 0.5

# libpq kwargs every pooled connection gets. Keepalives and tcp_user_timeout bound a
# blackholed network at ~QUERY_TIMEOUT_SECONDS instead of the kernel's ~15 minute
# retransmission limit; libpq ignores them on a Unix socket.
CONNECT_KWARGS: dict[str, int] = {
    # Closing a pool that timed out waits for its in-flight connects, which
    # against a blackholed host never return.
    "connect_timeout": int(OPEN_TIMEOUT_SECONDS),
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 3,
    "tcp_user_timeout": int(QUERY_TIMEOUT_SECONDS * 1000),
}


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
    """Split a SQL script into individual statements. Line comments (``--`` to end of
    line) are stripped first, so a semicolon *inside* a comment — e.g. schema.sql's
    "Conversations are scoped to it; users" — can't terminate a statement early and
    hand the driver a fragment starting with the leftover comment text. schema.sql has
    no dollar-quoted bodies, and no ``--`` or ``;`` inside string literals, so a plain
    ``;`` split of the comment-stripped code is safe. Whitespace-only chunks (e.g. the
    trailing newline after the last ``;``) are skipped."""
    code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    for chunk in code.split(";"):
        if chunk.strip():
            yield chunk.strip()


def apply_schema(conn, sql: str) -> None:
    """Run every statement of an idempotent schema on ``conn``. Each is a
    ``CREATE ... IF NOT EXISTS`` / ``INSERT ... ON CONFLICT DO NOTHING``, so this is
    a no-op once the database has converged."""
    for statement in iter_statements(sql):
        conn.execute(statement)


# pg_advisory_xact_lock key every boot-time DDL apply takes first ("Tenir" in ASCII).
# The stores' ``_pool_lock`` only serializes within one process: two api processes
# booting at once (a rolling update, >1 replica) raced the same CREATE ... IF NOT
# EXISTS and Postgres failed one with 40P01 or a pg_type unique violation, which
# reads as a rejected schema and aborts startup (XERK-1509).
SCHEMA_LOCK_KEY = 0x54656E6972


def lock_schema(conn) -> None:
    """Block until no other connection is applying DDL, holding the lock until this
    transaction ends. ``conn`` must not be autocommit, or it is released at once."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))


class SchemaApplyError(RuntimeError):
    """schema.sql was rejected by a reachable database (XERK-1409).

    Distinct from an unreachable database: that heals on its own, but a schema the
    database refuses — e.g. a migration that only fails on production data — leaves
    every session.start broken, so boot treats it as fatal rather than rolling out
    a pod that looks healthy and can't record."""


class SchemaLockTimeout(SchemaApplyError):
    """The boot schema apply gave up waiting for a table lock another transaction
    holds (XERK-1603). Fatal at boot like any ``SchemaApplyError``, but on the
    request path (a lazy re-open) it is transient, so it answers 503, not 500."""


class DatabaseUnavailable(RuntimeError):
    """The pool could not be opened: the last attempt, under OPEN_TIMEOUT_SECONDS
    ago, found the database unreachable (XERK-1434)."""


def _sever(conn) -> None:
    """Shut ``conn``'s socket down so a call blocked reading it fails at once with an
    OperationalError (no SQLSTATE: an outage). shutdown() acts on the socket, not the
    descriptor, so a dup is enough and libpq keeps owning (and later closing) its fd."""
    with contextlib.suppress(Exception):
        sock = socket.socket(fileno=os.dup(conn.fileno()))
        try:
            sock.shutdown(socket.SHUT_RDWR)
        finally:
            sock.close()


class _Watchdog:
    """Severs ``conn`` unless disarmed within ``seconds`` (XERK-1513)."""

    def __init__(self, conn, seconds: float, what: str) -> None:
        self._lock = threading.Lock()
        self._done = False
        self._timer = threading.Timer(seconds, self._fire, (conn, seconds, what))
        self._timer.daemon = True
        self._timer.start()

    def _fire(self, conn, seconds: float, what: str) -> None:
        # Under the lock so a disarm racing the timer can't hand the pool back a
        # connection that is severed after it was lent to someone else.
        with self._lock:
            if self._done:
                return
            self._done = True
            log.warning("database %s exceeded %.1fs; closing its connection", what, seconds)
            _sever(conn)

    def disarm(self) -> None:
        with self._lock:
            self._done = True
        self._timer.cancel()


def check_connection(conn) -> None:
    """The pool's check before lending a connection, bounded by CHECK_TIMEOUT_SECONDS:
    psycopg's own runs ``execute("")`` with no limit, and against a hung server each
    pooled connection trapped its borrower's thread indefinitely (XERK-1513)."""
    from psycopg_pool import ConnectionPool

    dog = _Watchdog(conn, CHECK_TIMEOUT_SECONDS, "connection check")
    try:
        ConnectionPool.check_connection(conn)
    finally:
        dog.disarm()


def _guarded_pool_class():
    """psycopg's ConnectionPool with a watchdog on every borrow and a breaker that
    shortens the connection wait while the database is unreachable (XERK-1513).
    Built lazily: psycopg_pool is the optional persistence extra."""
    from psycopg_pool import ConnectionPool

    return _guard(ConnectionPool)


@functools.cache
def _guard(ConnectionPool):  # noqa: N803 - the class being extended
    class GuardedPool(ConnectionPool):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # None until the boot schema applied: DDL on a large table can
            # legitimately outlast QUERY_TIMEOUT_SECONDS.
            self.borrow_timeout: float | None = None
            # Set when a connect attempt gave up after reconnect_timeout while no
            # borrow had succeeded for OPEN_TIMEOUT_SECONDS; cleared by the next
            # connection that opens or borrow that passes its check. One failed
            # reconnect is not an outage: at max_connections the replacement for a
            # dropped connection fails while the others still serve (XERK-1513 QA).
            self.unreachable = False
            self._last_ok = time.monotonic()
            self._watchdogs: dict[int, _Watchdog] = {}
            super().__init__(*args, configure=self._connected, **kwargs)

        def _reachable(self) -> None:
            self._last_ok = time.monotonic()
            if self.unreachable:
                log.info("database reachable again (pool %s)", self.name)
                self.unreachable = False

        def _connected(self, conn) -> None:
            conn.execute(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_SECONDS * 1000)}")
            if not conn.autocommit:
                conn.commit()
            self._reachable()

        def reconnect_failed(self) -> None:
            if time.monotonic() - self._last_ok >= OPEN_TIMEOUT_SECONDS and not self.unreachable:
                log.warning("database unreachable (pool %s); failing requests fast", self.name)
                self.unreachable = True
            super().reconnect_failed()

        def getconn(self, timeout: float | None = None):
            if self.unreachable:
                timeout = min(self.timeout if timeout is None else timeout, OUTAGE_WAIT_SECONDS)
            conn = super().getconn(timeout)
            self._reachable()
            if self.borrow_timeout is not None:
                self._watchdogs[id(conn)] = _Watchdog(conn, self.borrow_timeout, "query")
            return conn

        def putconn(self, conn) -> None:
            dog = self._watchdogs.pop(id(conn), None)
            if dog is not None:
                dog.disarm()
            super().putconn(conn)

    return GuardedPool


class PoolOpener:
    """Opens a store's connection pool with bounded waits (XERK-1434).

    A pool that can't open within OPEN_TIMEOUT_SECONDS is closed and its error is
    shared with every caller for that long, instead of each one queueing another
    full wait behind the store's lock. The caller holds its own lock and caches
    the returned pool."""

    def __init__(self, dsn: str, name: str) -> None:
        self._dsn = dsn
        self._name = name
        self._failure: tuple[float, Exception] | None = None

    def open(self, init: Callable[[Any], None]):
        """A ready pool with ``init(pool)`` applied; closed again if either fails.
        Only a failure to open is remembered: an ``init`` error (a rejected schema)
        is retried on the very next call."""
        failure = self._failure
        if failure is not None and time.monotonic() - failure[0] < OPEN_TIMEOUT_SECONDS:
            raise DatabaseUnavailable(f"database unreachable: {failure[1]}") from failure[1]
        log.info("opening Postgres connection pool (%s)", self._name)
        pool = _guarded_pool_class()(
            self._dsn,
            name=self._name,
            open=False,
            kwargs=dict(CONNECT_KWARGS),
            # Every request's wait for a connection, too: with the database gone
            # after the pool opened, psycopg's 30s default held a worker thread per
            # request, and 50 of them stalled every sync endpoint for 30-60s.
            timeout=OPEN_TIMEOUT_SECONDS,
            # Pooled connections outlive a Postgres restart; without a check each
            # one fails its next borrower once (AdminShutdown) before it is dropped.
            check=check_connection,
            # Give up a failed reconnect after this long, so the next request's
            # connect attempt starts fresh. psycopg's 5-minute default kept each
            # broken connection on exponential backoff (1, 2, 4 ... 64s), and the
            # first request succeeded up to a minute after the database was back
            # (XERK-1513). Giving up also trips the pool's outage breaker.
            reconnect_timeout=OPEN_TIMEOUT_SECONDS,
        )
        try:
            try:
                pool.open(wait=True, timeout=OPEN_TIMEOUT_SECONDS)
            except Exception as exc:
                self._failure = (time.monotonic(), exc)
                raise
            init(pool)
        except BaseException:
            pool.close()
            raise
        pool.borrow_timeout = QUERY_TIMEOUT_SECONDS
        return pool


def _is_connection_lost(conn, exc: BaseException) -> bool:
    """True when a statement failed because the connection itself went away (the
    server restarting mid-apply) — that heals on its own, so it is not a rejected
    schema. Deliberately NOT "any psycopg OperationalError": that class also covers
    real rejections such as 54000 (index row too large) or 53100 (disk full), and
    treating those as an outage booted a Ready pod on a broken schema again."""
    if getattr(conn, "broken", False):
        return True
    return _is_connection_sqlstate(getattr(exc, "sqlstate", None) or "")


def _is_connection_sqlstate(sqlstate: str) -> bool:
    # Class 08: connection exception; 57P01-57P03: admin/crash shutdown, cannot connect.
    return sqlstate.startswith("08") or sqlstate in ("57P01", "57P02", "57P03")


def database_error_types() -> tuple[type[BaseException], ...]:
    """The exception classes ``is_database_unavailable`` may accept, for registering
    handlers by class. psycopg's only exist when the persistence extra is installed."""
    try:
        import psycopg
        from psycopg_pool import PoolTimeout
    except ImportError:
        return (DatabaseUnavailable,)
    return (DatabaseUnavailable, SchemaLockTimeout, PoolTimeout, psycopg.OperationalError)


def is_database_unavailable(exc: BaseException) -> bool:
    """True when ``exc`` means "the database can't be reached right now" — a
    retryable outage the API answers with 503 rather than a 500 (XERK-1510).

    Covers ``DatabaseUnavailable`` (the pool can't open), psycopg_pool's
    ``PoolTimeout`` (no connection within the request's wait) and a connection-level
    ``OperationalError``: a lost/refused connection carries no SQLSTATE (the server
    never answered) or a class-08/57P0x one. Other OperationalErrors (disk full, a
    too-large index row) are real faults and stay 500s, as in ``_is_connection_lost``."""
    if isinstance(exc, (DatabaseUnavailable, SchemaLockTimeout)):
        return True
    try:
        import psycopg
        from psycopg_pool import PoolTimeout
    except ImportError:  # in-memory install: no Postgres to be unavailable
        return False
    if isinstance(exc, PoolTimeout):
        return True
    if isinstance(exc, psycopg.OperationalError):
        sqlstate = exc.sqlstate
        return sqlstate is None or _is_connection_sqlstate(sqlstate)
    return False


def is_retryable_write(exc: BaseException) -> bool:
    """True when a write the session holds for retry should stay held: an outage
    (``is_database_unavailable``), or a statement the server ended at
    STATEMENT_TIMEOUT_SECONDS (57014) — a lock held past it, e.g. another replica's
    boot DDL, dropped live transcript segments and the recording's audio key
    (XERK-1513 QA). Only for writes that must land; elsewhere 57014 stays a fault."""
    return is_database_unavailable(exc) or getattr(exc, "sqlstate", None) == "57014"


# Boot DDL waits at most this long for each table lock (XERK-1603). Even
# ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` takes ACCESS EXCLUSIVE before it checks
# the column, so behind one idle-in-transaction reader it waited forever — and every
# request on that table queued behind it in the lock queue. Bounded, a blocked apply
# gives up, the queue drains, and it retries after a backoff; requests stall at most
# SCHEMA_LOCK_TIMEOUT_MS per attempt instead of until the reader ends.
SCHEMA_LOCK_TIMEOUT_MS = 2000
SCHEMA_LOCK_ATTEMPTS = 3
SCHEMA_LOCK_BACKOFF_SECONDS = 1.0


def _is_lock_timeout(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "55P03"  # lock_not_available


def apply_boot_schema(pool, extra: Sequence[str] = ()) -> None:  # pragma: no cover - live DB
    """Apply schema.sql, then a store's ``extra`` DDL, in one transaction under the
    cross-process schema lock.

    Both stores apply through here (XERK-1430): the users DDL references households,
    which on an empty database only exists once schema.sql ran, so the user store
    applying its own DDL alone failed (UndefinedTable) whenever it opened first —
    and its env-admin seed with it. A statement the database rejects raises
    ``SchemaApplyError``; failing to get a connection, or losing it mid-apply,
    propagates as-is (database unreachable).

    A table lock still held by another transaction after SCHEMA_LOCK_ATTEMPTS
    bounded waits is a ``SchemaLockTimeout`` (a ``SchemaApplyError``), so boot fails
    visibly (and is retried by the restart) rather than serving on a schema it
    couldn't apply."""
    path = find_schema_file()
    if path is None:
        log.warning("schema.sql not found; skipping it in the boot schema apply")
    sql = path.read_text(encoding="utf-8") if path is not None else ""
    for attempt in range(1, SCHEMA_LOCK_ATTEMPTS + 1):
        try:
            _apply_boot_schema_once(pool, sql, extra, path)
            break
        except Exception as exc:
            if not _is_lock_timeout(exc):
                raise
            if attempt == SCHEMA_LOCK_ATTEMPTS:
                raise SchemaLockTimeout(
                    f"boot schema (schema.sql from {path}) could not take a table lock in"
                    f" {SCHEMA_LOCK_ATTEMPTS} attempts; a long-running or idle-in-transaction"
                    f" session holds it (see pg_stat_activity): {exc}"
                ) from exc
            log.warning(
                "boot schema apply timed out waiting for a table lock (attempt %d/%d);"
                " retrying: %s",
                attempt,
                SCHEMA_LOCK_ATTEMPTS,
                exc,
            )
            time.sleep(SCHEMA_LOCK_BACKOFF_SECONDS * attempt)
    if path is not None:
        log.info("applied idempotent schema from %s on pool open", path)


def _apply_boot_schema_once(pool, sql: str, extra: Sequence[str], path) -> None:
    with pool.connection() as conn:
        try:
            # DDL on a large table, or waiting out another replica's apply, may
            # legitimately outlast the request-path STATEMENT_TIMEOUT_SECONDS.
            conn.execute("SET LOCAL statement_timeout = 0")
            lock_schema(conn)
            # After the advisory lock, not before: waiting on another replica's apply
            # is bounded already, since every statement it runs is bounded by this.
            # LOCAL, so it ends with this transaction and never reaches request traffic.
            conn.execute(
                "SELECT set_config('lock_timeout', %s, true)", (f"{SCHEMA_LOCK_TIMEOUT_MS}ms",)
            )
            apply_schema(conn, sql)
            for statement in extra:
                conn.execute(statement)
        except Exception as exc:
            if _is_connection_lost(conn, exc) or _is_lock_timeout(exc):
                raise
            raise SchemaApplyError(
                f"boot schema (schema.sql from {path}) failed to apply: {exc}"
            ) from exc


# Listing and search render a count and a duration per conversation, so they aggregate
# the page's segments in SQL rather than loading every segment, cue and song row just to
# count them (XERK-1524). The page is cut first and only its rows are aggregated: a
# lateral join outside the LIMIT would run for every skipped or sorted row. Duration
# matches Conversation.duration_ms (0 when empty), in bigint so a span past 2^31 ms can't
# 500 the listing. id breaks started_at ties so paging never repeats or drops a row.
def _summary_page(page_sql: str) -> str:
    return f"""
        SELECT c.*, agg.segment_count, agg.duration_ms
        FROM ({page_sql}) c
        CROSS JOIN LATERAL (
            SELECT count(*)::int AS segment_count,
                   COALESCE(max(s.end_ms)::bigint - min(s.start_ms), 0) AS duration_ms
            FROM segments s WHERE s.conversation_id = c.id
        ) agg
        ORDER BY c.started_at DESC, c.id
    """


class SqlConversationStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None
        # Serializes pool open + schema apply. Without it, concurrent first callers
        # each open a pool and run the DDL in parallel, which Postgres deadlocks on.
        self._pool_lock = threading.Lock()
        self._opener = PoolOpener(dsn, "conversations")

    def open(self) -> None:  # pragma: no cover - requires psycopg + a live database
        """Eagerly open the pool and apply the schema at boot.

        A rejected schema raises ``SchemaApplyError`` so startup fails and the pod
        crashloops instead of reporting Ready. Any other failure (database not
        reachable yet) is only logged: the lazy path retries on next use and
        ``/ready`` reports it meanwhile."""
        try:
            self._ensure_pool()
        except (SchemaApplyError, ImportError):
            # A missing driver is a permanent misconfiguration, not a DB outage.
            raise
        except Exception as exc:  # noqa: BLE001 - unreachable DB is non-fatal at boot
            log.warning("database not reachable at startup; will retry lazily: %s", exc)

    def _ensure_pool(self):  # pragma: no cover - requires psycopg + a live database
        if self._pool is not None:
            return self._pool
        with self._pool_lock:
            if self._pool is not None:
                return self._pool
            # Self-heal schema drift on boot. Postgres only applies schema.sql on a
            # FRESH data volume (docker-entrypoint-initdb.d), so a database created
            # before an additive change — e.g. the `cues` table (XERK-81) that reads
            # like get() JOIN against — never gets it, and every such read (and the
            # session.start create() that calls get()) then fails "relation does not
            # exist", killing transcription. Re-applying the idempotent schema here
            # converges an old data dir without a manual migration.
            # The pool is cached only once the schema applied: caching it first meant
            # one failed apply was never retried and every later call ran against the
            # broken schema with nothing reporting it (XERK-1409).
            self._pool = self._opener.open(self._apply_schema)
        return self._pool

    def _apply_schema(self, pool) -> None:  # pragma: no cover - requires a live database
        apply_boot_schema(pool)

    @staticmethod
    def _row_to_conversation(  # pragma: no cover
        row, segments: list[Segment], cues: list[Cue], songs: list[Song]
    ) -> Conversation:
        return Conversation(
            id=row["id"],
            household=row["household"],
            owner=row.get("owner"),
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
            songs=songs,
        )

    def create(  # pragma: no cover - requires a live database
        self,
        household: str,
        conversation_id: str,
        *,
        owner: str | None = None,
        mic_source: str | None = None,
        source_lang: str | None = None,
    ) -> Conversation:
        with self._ensure_pool().connection() as conn:
            # A resume keeps the row's original owner and start (a resume never
            # re-owns a recording) but reopens it: a finished recording being
            # recorded again reads live, not 'ready' with its old ended_at
            # (XERK-1502). Mirrors the in-memory store. The household guard keeps
            # an id collision from touching another household's row.
            conn.execute(
                """
                INSERT INTO conversations
                    (id, household, owner, mic_source, source_lang, started_at, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'live')
                ON CONFLICT (id) DO UPDATE SET status = 'live', ended_at = NULL
                 WHERE conversations.household = EXCLUDED.household
                """,
                (conversation_id, household, owner, mic_source, source_lang, utcnow()),
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
                    (segment_id, conversation_id, text, start_ms, end_ms, lang, translation)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (segment_id) DO UPDATE SET
                    text = EXCLUDED.text, start_ms = EXCLUDED.start_ms,
                    end_ms = EXCLUDED.end_ms, lang = EXCLUDED.lang,
                    translation = EXCLUDED.translation
                """,
                (
                    segment.segment_id,
                    conversation_id,
                    segment.text,
                    segment.start_ms,
                    segment.end_ms,
                    segment.lang,
                    segment.translation,
                ),
            )

    def set_segment_translation(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str, segment_id: str, translation: str
    ) -> None:
        with self._ensure_pool().connection() as conn:
            conn.execute(
                "UPDATE segments SET translation = %s "
                "WHERE segment_id = %s AND conversation_id = %s",
                (translation, segment_id, conversation_id),
            )

    def add_cue(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str, cue: Cue
    ) -> None:
        with self._ensure_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO cues
                    (cue_id, conversation_id, title, body, at_ms, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (cue_id) DO UPDATE SET
                    title = EXCLUDED.title, body = EXCLUDED.body, at_ms = EXCLUDED.at_ms,
                    source = EXCLUDED.source
                """,
                (cue.cue_id, conversation_id, cue.title, cue.body, cue.at_ms, cue.source),
            )

    def add_song(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str, song: Song
    ) -> None:
        with self._ensure_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO songs
                    (song_id, conversation_id, title, artist, at_ms, duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (song_id) DO UPDATE SET
                    title = EXCLUDED.title, artist = EXCLUDED.artist,
                    at_ms = EXCLUDED.at_ms, duration_ms = EXCLUDED.duration_ms
                """,
                (
                    song.song_id,
                    conversation_id,
                    song.title,
                    song.artist,
                    song.at_ms,
                    song.duration_ms,
                ),
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
        self, household: str, conversation_id: str, *, owner: str | None = None
    ) -> Conversation | None:
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            # Scope dict rows to the cursor, never the pooled connection: psycopg's
            # pool doesn't reset row_factory on return, so mutating the connection
            # leaks dict rows into the next borrower (e.g. households()'s r[0]).
            cur = conn.cursor(row_factory=dict_row)
            # Owner scope (XERK-651): owner=None is admin/internal (no filter); a member
            # id restricts to their own rows, so a NULL-owner or another user's row reads
            # back as missing → the router turns that into a 404 (ids don't leak).
            if owner is None:
                row = cur.execute(
                    "SELECT * FROM conversations WHERE household = %s AND id = %s",
                    (household, conversation_id),
                ).fetchone()
            else:
                row = cur.execute(
                    "SELECT * FROM conversations WHERE household = %s AND id = %s AND owner = %s",
                    (household, conversation_id, owner),
                ).fetchone()
            if row is None:
                return None
            return self._assemble(cur, [row])[0]

    def _assemble(self, cur, rows) -> list[Conversation]:  # pragma: no cover - live database
        """Build conversations from their rows, reading every child table once for all of
        them on the caller's cursor. Listing used to call get() per row — a pool borrow
        plus three reads each — so one /conversations queued 1+N times for the small pool
        (XERK-1518)."""
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        segs: dict[str, list[Segment]] = {i: [] for i in ids}
        cues: dict[str, list[Cue]] = {i: [] for i in ids}
        songs: dict[str, list[Song]] = {i: [] for i in ids}
        for r in cur.execute(
            "SELECT * FROM segments WHERE conversation_id = ANY(%s) ORDER BY start_ms, segment_id",
            (ids,),
        ).fetchall():
            segs[r["conversation_id"]].append(self._row_to_segment(r))
        for r in cur.execute(
            "SELECT * FROM cues WHERE conversation_id = ANY(%s) ORDER BY at_ms, cue_id", (ids,)
        ).fetchall():
            cues[r["conversation_id"]].append(self._row_to_cue(r))
        for r in cur.execute(
            "SELECT * FROM songs WHERE conversation_id = ANY(%s) ORDER BY at_ms, song_id", (ids,)
        ).fetchall():
            songs[r["conversation_id"]].append(self._row_to_song(r))
        return [
            self._row_to_conversation(r, segs[r["id"]], cues[r["id"]], songs[r["id"]]) for r in rows
        ]

    @staticmethod
    def _row_to_summary(row) -> ConversationSummary:  # pragma: no cover - live database
        return ConversationSummary(
            id=row["id"],
            household=row["household"],
            owner=row.get("owner"),
            mic_source=row["mic_source"],
            source_lang=row["source_lang"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            status=coerce_status(row["status"], ended=row["ended_at"] is not None),
            audio_key=row["audio_key"],
            segment_count=row["segment_count"],
            duration_ms=row["duration_ms"],
        )

    @staticmethod
    def _row_to_segment(row) -> Segment:  # pragma: no cover - requires a live database
        return Segment(
            segment_id=row["segment_id"],
            text=row["text"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            lang=row["lang"],
            translation=row["translation"],
        )

    @staticmethod
    def _row_to_cue(row) -> Cue:  # pragma: no cover - requires a live database
        return Cue(
            cue_id=row["cue_id"],
            title=row["title"],
            body=row["body"],
            at_ms=row["at_ms"],
            source=row["source"],
        )

    @staticmethod
    def _row_to_song(row) -> Song:  # pragma: no cover - requires a live database
        return Song(
            song_id=row["song_id"],
            title=row["title"],
            artist=row["artist"],
            at_ms=row["at_ms"],
            duration_ms=row["duration_ms"],
        )

    def list(  # pragma: no cover - requires a live database
        self, household: str, *, owner: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[ConversationSummary]:
        from psycopg.rows import dict_row

        # Owner scope (XERK-651): owner=None is the admin view (whole household); a member
        # id restricts to their own rows. The owner index carries (household, owner, ...).
        where = "c.household = %s" if owner is None else "c.household = %s AND c.owner = %s"
        params: tuple = (household,) if owner is None else (household, owner)
        with self._ensure_pool().connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            rows = cur.execute(
                _summary_page(
                    f"""
                    SELECT * FROM conversations c WHERE {where}
                    ORDER BY c.started_at DESC, c.id LIMIT %s OFFSET %s
                    """
                ),
                (*params, limit, offset),
            ).fetchall()
            return [self._row_to_summary(r) for r in rows]

    def search(  # pragma: no cover - requires a live database
        self,
        household: str,
        query: str,
        *,
        owner: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationSummary]:
        from psycopg.rows import dict_row

        # Owner scope (XERK-651): owner=None searches the whole household (admin); a member
        # id restricts to rows they own, so search can never surface another user's match.
        owner_clause = "" if owner is None else "AND c.owner = %s"
        owner_param: tuple = () if owner is None else (owner,)
        with self._ensure_pool().connection() as conn:
            # Match per-row so the functional FTS index on segments
            # (to_tsvector('simple', text), schema.sql) can serve the query — a
            # tsvector built over an aggregate (string_agg) can't use that index and
            # forces a full scan + per-query recompute. Rank by recency of the
            # matching conversation; relevance ranking can layer on later if needed.
            cur = conn.cursor(row_factory=dict_row)
            rows = cur.execute(
                _summary_page(
                    f"""
                    SELECT c.* FROM conversations c
                    WHERE c.household = %s {owner_clause}
                      AND EXISTS (
                          SELECT 1 FROM segments s
                          WHERE s.conversation_id = c.id
                            AND to_tsvector('simple', s.text)
                                @@ websearch_to_tsquery('simple', %s)
                      )
                    ORDER BY c.started_at DESC, c.id LIMIT %s OFFSET %s
                    """
                ),
                (household, *owner_param, query, limit, offset),
            ).fetchall()
            return [self._row_to_summary(r) for r in rows]

    def delete(  # pragma: no cover - requires a live database
        self, household: str, conversation_id: str
    ) -> bool:
        with self._ensure_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM conversations WHERE household = %s AND id = %s",
                (household, conversation_id),
            )
            return cur.rowcount > 0

    def ready(self) -> None:
        """Readiness probe: one round trip, never waiting longer than
        OPEN_TIMEOUT_SECONDS for a connection (the request path keeps the pool's own
        timeout). Raises when the database is unreachable."""
        with self._ensure_pool().connection(timeout=OPEN_TIMEOUT_SECONDS) as conn:
            conn.execute("SELECT 1")

    def households(self) -> list[str]:  # pragma: no cover - requires a live database
        with self._ensure_pool().connection() as conn:
            rows = conn.execute("SELECT DISTINCT household FROM conversations").fetchall()
        return [r[0] for r in rows]

    def finish_stale(self) -> int:  # pragma: no cover - requires a live database
        """Close out conversations left ``live`` by a previous process (XERK-236).

        Only a graceful shutdown finalizes live sessions; an OOM kill, a host
        reboot or an overrun stop leaves the row `live` with no `ended_at`, and
        nothing ever came back for it — so it showed as permanently recording in
        every client's history. `ended_at` falls back to the last segment's
        wall-clock end, else the row's own start, so the duration a client
        renders is the best available truth rather than "now".
        """
        with self._ensure_pool().connection() as conn:
            rows = conn.execute(
                """
                UPDATE conversations
                   SET status = 'ready',
                       ended_at = COALESCE(
                           ended_at,
                           started_at + (
                               (SELECT MAX(end_ms) FROM segments
                                 WHERE segments.conversation_id = conversations.id)
                               * INTERVAL '1 millisecond'
                           ),
                           started_at
                       )
                 WHERE status = 'live'
             RETURNING id
                """
            ).fetchall()
        return len(rows)
