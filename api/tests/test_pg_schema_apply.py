"""Regression: the api must self-heal an existing Postgres data dir on boot.

Postgres only runs ``schema.sql`` on a FRESH volume (docker-entrypoint-initdb.d),
so a database created before an additive change never gets it. The cue work
(XERK-81) added a ``cues`` table that ``SqlConversationStore.get`` reads
unconditionally; ``create`` (the session.start path) calls ``get``. On an
upgraded-in-place database the ``cues`` table was missing, so every ``get`` — and
thus every ``session.start`` — raised ``relation "cues" does not exist``, which
surfaced to clients as ``could not start session`` and killed transcription
entirely.

The fix applies the idempotent schema on connection-pool open. The pooled paths
need a live database, so these tests exercise the real splitting/apply logic and
the pool wiring against fake pools — no database. CI installs the
``[persistence]`` extra (psycopg), which the error-classification tests that
raise real psycopg errors need; they skip without it.
"""

from __future__ import annotations

import contextlib
import sys
import time
import types

import pytest

from api.persistence.postgres import (
    apply_schema,
    find_schema_file,
    iter_statements,
)


class _NoRows:
    """An empty query result, for the user store's post-apply duplicate-username read."""

    def fetchall(self) -> list:
        return []


class _RecordingConn:
    """Captures the statements a schema-apply runs, normalized to single spaces."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None) -> _NoRows:
        self.statements.append(" ".join(sql.split()))
        return _NoRows()


def _creates_cues(statements: list[str]) -> bool:
    # Statements carry their leading comment block, so match the DDL as a substring
    # (with the opening paren, so a comment merely mentioning "cues" can't match).
    return any("CREATE TABLE IF NOT EXISTS CUES (" in s.upper() for s in statements)


def test_iter_statements_splits_and_skips_comment_only_chunks() -> None:
    sql = (
        "-- a leading comment\n"
        "CREATE TABLE IF NOT EXISTS a (id TEXT);\n"
        "\n"
        "-- trailing note only\n"
        "CREATE INDEX IF NOT EXISTS a_idx ON a (id);\n"
    )
    statements = list(iter_statements(sql))
    assert len(statements) == 2
    # Comments are stripped, so each statement is pure SQL the driver can run — no
    # leading comment text that would confuse the parser.
    assert statements[0] == "CREATE TABLE IF NOT EXISTS a (id TEXT)"
    assert statements[1] == "CREATE INDEX IF NOT EXISTS a_idx ON a (id)"
    # The dangling chunk after the last ';' (whitespace + a bare comment) is dropped,
    # so no empty statement reaches the driver.


def test_iter_statements_ignores_semicolons_inside_comments() -> None:
    # Regression: a ';' inside a line comment must NOT split the statement. schema.sql
    # has exactly this ("...scoped to it; users") right before the households table.
    # The naive split cut there and handed the driver a fragment beginning with the
    # leftover comment word ("users ... CREATE TABLE ..."), which Postgres rejected as
    # `syntax error at or near "users"`, aborting the whole boot schema-apply.
    sql = (
        "-- The boundary is scoped to it; users authenticate into it.\n"
        "CREATE TABLE IF NOT EXISTS households (id TEXT PRIMARY KEY);\n"
    )
    statements = list(iter_statements(sql))
    assert statements == ["CREATE TABLE IF NOT EXISTS households (id TEXT PRIMARY KEY)"]


def test_find_schema_file_locates_repo_schema() -> None:
    path = find_schema_file()
    assert path is not None, "schema.sql should be resolvable from the repo"
    text = path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS cues" in text


def test_apply_schema_creates_the_cues_table() -> None:
    # The actual production schema drives this — the exact file the fix ships and
    # applies on boot. Before the fix nothing created `cues` outside a fresh volume.
    path = find_schema_file()
    assert path is not None
    conn = _RecordingConn()

    apply_schema(conn, path.read_text(encoding="utf-8"))

    assert _creates_cues(conn.statements), "boot schema apply must create the cues table"
    # Idempotent guard: every statement is safe to re-run on a converged DB — a
    # CREATE/INSERT guarded by IF NOT EXISTS / ON CONFLICT, an ALTER COLUMN ...
    # DROP NOT NULL (a no-op when the column is already nullable, XERK-650), or a
    # WHERE-guarded backfill UPDATE that only rewrites still-unset rows and so
    # converges after its first run (the conversations.owner backfill, XERK-651).
    assert all(
        "IF NOT EXISTS" in s
        or "ON CONFLICT" in s
        or s.upper().startswith("INSERT")
        or "DROP NOT NULL" in s.upper()
        or (s.upper().startswith("UPDATE") and "WHERE" in s.upper())
        for s in conn.statements
    )


def test_ensure_pool_applies_schema_on_open(monkeypatch) -> None:
    """Opening the pool self-applies the schema, so the cues table exists before the
    first get()/create() reads it — the wiring that keeps session.start alive."""
    conn = _RecordingConn()

    class _FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, dsn: str, open: bool = True, **_: object) -> None:  # noqa: A002
            self.dsn = dsn

        def open(self, wait: bool = False, timeout: float = 30.0) -> None:
            pass

        @contextlib.contextmanager
        def connection(self):
            yield conn

    fake_mod = types.ModuleType("psycopg_pool")
    fake_mod.ConnectionPool = _FakePool
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_mod)

    from api.persistence.postgres import SqlConversationStore

    store = SqlConversationStore("postgresql://unused")
    pool = store._ensure_pool()

    assert isinstance(pool, _FakePool)
    assert _creates_cues(conn.statements), "pool open must apply the schema (incl. cues)"
    # Second call reuses the pool and does NOT re-run the schema.
    before = len(conn.statements)
    store._ensure_pool()
    assert len(conn.statements) == before


class _FailingConn:
    """A connection whose every statement fails, like an ALTER that production
    data rejects (XERK-1409)."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, sql: str, params: object = None) -> None:
        self.calls += 1
        raise RuntimeError("cannot add foreign key")


def _install_fake_pool(monkeypatch, conn) -> list:
    """Swap in a psycopg_pool whose connections yield ``conn``; returns the pools
    created, so a test can see which were closed."""
    pools: list = []

    class _FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, dsn: str, open: bool = True, **_: object) -> None:  # noqa: A002
            self.closed = False
            pools.append(self)

        def open(self, wait: bool = False, timeout: float = 30.0) -> None:
            pass

        @contextlib.contextmanager
        def connection(self):
            yield conn

        def close(self) -> None:
            self.closed = True

    fake_mod = types.ModuleType("psycopg_pool")
    fake_mod.ConnectionPool = _FakePool
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_mod)
    return pools


def test_failed_schema_apply_is_retried_not_cached(monkeypatch) -> None:
    """Regression (XERK-1409): the pool was cached BEFORE the schema apply, so one
    failed apply was never retried and every later call ran against the broken
    schema while /ready (and the boot probe) reported nothing wrong."""
    from api.persistence.postgres import SchemaApplyError, SqlConversationStore

    conn = _FailingConn()
    pools = _install_fake_pool(monkeypatch, conn)
    store = SqlConversationStore("postgresql://unused")

    with pytest.raises(SchemaApplyError):
        store._ensure_pool()
    with pytest.raises(SchemaApplyError):
        store._ensure_pool()

    assert conn.calls == 2, "the second call must re-attempt the schema apply"
    assert all(p.closed for p in pools), "a pool whose schema failed must be closed"
    assert store._pool is None


def _serve_store(monkeypatch, store) -> None:
    """Make the app (boot + /ready) use ``store`` as its conversation store."""
    from api import main, readiness

    monkeypatch.setattr(main, "get_conversation_store", lambda: store)
    monkeypatch.setattr(readiness, "get_conversation_store", lambda: store)


def test_boot_fails_when_the_database_rejects_the_schema(monkeypatch) -> None:
    """XERK-1409: a schema the database rejects must abort startup (the pod
    crashloops) instead of booting a healthy-looking api whose every session.start
    fails."""
    from fastapi.testclient import TestClient

    from api.main import app
    from api.persistence.postgres import SchemaApplyError, SqlConversationStore

    _install_fake_pool(monkeypatch, _FailingConn())
    _serve_store(monkeypatch, SqlConversationStore("postgresql://unused"))

    with pytest.raises(SchemaApplyError), TestClient(app):
        pass


def test_boot_survives_an_unreachable_database_and_ready_reports_it(monkeypatch) -> None:
    """An unreachable database stays non-fatal at boot (it heals on its own), but
    /ready must report it — and a probe after the failure window retries the pool +
    schema (within it, the last verdict is shared: XERK-1434)."""
    from fastapi.testclient import TestClient

    from api.main import app
    from api.persistence import postgres
    from api.persistence.postgres import OPEN_TIMEOUT_SECONDS, SqlConversationStore

    attempts = []

    class _Down:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, dsn: str, open: bool = True, **_: object) -> None:  # noqa: A002
            attempts.append(self)

        def open(self, wait: bool = False, timeout: float = 30.0) -> None:
            raise TimeoutError(f"pool initialization incomplete after {timeout} sec")

        def close(self) -> None:
            pass

    fake_mod = types.ModuleType("psycopg_pool")
    fake_mod.ConnectionPool = _Down
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_mod)
    _serve_store(monkeypatch, SqlConversationStore("postgresql://unused"))

    with TestClient(app) as client:
        before = len(attempts)
        resp = client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["checks"]["conversations"] == "error"
        assert len(attempts) == before, "within the window /ready must not re-wait"

        now = time.monotonic()
        monkeypatch.setattr(postgres.time, "monotonic", lambda: now + OPEN_TIMEOUT_SECONDS + 1)
        resp = client.get("/ready")
        assert resp.status_code == 503
        assert len(attempts) == before + 1, "/ready must retry opening the pool"


def test_user_store_failed_schema_apply_is_retried_not_cached(monkeypatch) -> None:
    """Same regression in SqlUserStore (XERK-1409): a failed ensure-schema must
    leave no cached pool, so the next call retries it."""
    from api.auth.sql_users import SqlUserStore

    conn = _FailingConn()
    pools = _install_fake_pool(monkeypatch, conn)
    store = SqlUserStore("postgresql://unused")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            store._ensure_pool()

    assert conn.calls == 2, "the second call must re-attempt the schema apply"
    assert all(p.closed for p in pools)
    assert store._pool is None


@pytest.mark.parametrize(
    "store_path", ["persistence.postgres:SqlConversationStore", "auth.sql_users:SqlUserStore"]
)
def test_concurrent_first_use_applies_the_schema_once(monkeypatch, store_path) -> None:
    """Concurrent first callers must not each open a pool and run the DDL in
    parallel — on real Postgres that deadlocks and reads as a rejected schema
    (QA on XERK-1409). The first caller applies; the rest wait and reuse it."""
    import importlib
    import threading
    import time

    module, cls = store_path.split(":")
    store_cls = getattr(importlib.import_module(f"api.{module}"), cls)

    in_apply = 0
    max_in_apply = 0
    guard = threading.Lock()

    class _SlowConn:
        def execute(self, sql: str, params: object = None) -> _NoRows:
            nonlocal in_apply, max_in_apply
            with guard:
                in_apply += 1
                max_in_apply = max(max_in_apply, in_apply)
            time.sleep(0.001)
            with guard:
                in_apply -= 1
            return _NoRows()

    pools = _install_fake_pool(monkeypatch, _SlowConn())
    store = store_cls("postgresql://unused")
    start = threading.Barrier(8)

    def first_use() -> None:
        start.wait()
        store._ensure_pool()

    threads = [threading.Thread(target=first_use) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(pools) == 1, "only one pool may be opened"
    assert max_in_apply == 1, "schema statements must never run concurrently"


@pytest.mark.parametrize(
    "store_path", ["persistence.postgres:SqlConversationStore", "auth.sql_users:SqlUserStore"]
)
def test_schema_apply_takes_the_cross_process_lock_first(monkeypatch, store_path) -> None:
    """``_pool_lock`` only serializes one process: two api processes booting at once
    raced the DDL and Postgres failed one, aborting its startup (XERK-1509). Both
    stores must take the shared advisory lock before any DDL."""
    import importlib

    from api.persistence.postgres import SCHEMA_LOCK_KEY

    module, cls = store_path.split(":")
    store_cls = getattr(importlib.import_module(f"api.{module}"), cls)
    calls: list[tuple[str, object]] = []

    class _Conn:
        def execute(self, sql: str, params: object = None) -> _NoRows:
            calls.append((" ".join(sql.split()), params))
            return _NoRows()

    _install_fake_pool(monkeypatch, _Conn())
    store_cls("postgresql://unused")._ensure_pool()

    # The request-path statement_timeout is lifted first: waiting out another
    # replica's apply, or DDL on a large table, may outlast it (XERK-1513).
    assert calls[0] == ("SET LOCAL statement_timeout = 0", None)
    assert calls[1] == ("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
    assert len(calls) > 2, "the DDL runs after the lock"


def test_user_store_applies_schema_sql_before_its_own_ddl(monkeypatch) -> None:
    """The users DDL references households, which on an empty database only exists
    once schema.sql ran. The user store applying its own DDL alone failed with
    UndefinedTable whenever it opened before the conversation store — and the env-admin
    seed with it, so a DB-down boot never seeded the admin (XERK-1430)."""
    from api.auth.sql_users import SqlUserStore

    conn = _RecordingConn()
    _install_fake_pool(monkeypatch, conn)
    SqlUserStore("postgresql://unused")._ensure_pool()

    households = next(
        i for i, s in enumerate(conn.statements) if "TABLE IF NOT EXISTS households" in s
    )
    users = [
        i
        for i, s in enumerate(conn.statements)
        if s.upper().startswith(("CREATE", "ALTER")) and " users " in f"{s} "
    ]
    assert users and households < min(users)


class _SqlStateError(Exception):
    """Stands in for a psycopg error carrying a SQLSTATE."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


class _RaisingConn:
    def __init__(self, exc: Exception, broken: bool = False) -> None:
        self.exc = exc
        self.broken = broken

    def execute(self, sql: str, params: object = None) -> None:
        raise self.exc


@pytest.mark.parametrize(
    "error", ["ProgramLimitExceeded", "DiskFull", "DeadlockDetected", "UniqueViolation"]
)
def test_database_rejections_are_schema_errors(monkeypatch, error) -> None:
    """A rejection that psycopg classes as OperationalError (54000 index row too
    large, 53100 disk full, 40P01 deadlock) is still a broken schema and must abort
    boot — not be mistaken for an outage (QA on XERK-1409). Real psycopg error
    classes, so a classifier keyed on OperationalError would fail this."""
    errors = pytest.importorskip("psycopg.errors")
    from api.persistence.postgres import SchemaApplyError, SqlConversationStore

    _install_fake_pool(monkeypatch, _RaisingConn(getattr(errors, error)("rejected")))
    store = SqlConversationStore("postgresql://unused")

    with pytest.raises(SchemaApplyError):
        store.open()


@pytest.mark.parametrize(("sqlstate", "broken"), [("57P01", False), ("08006", False), ("", True)])
def test_connection_lost_mid_apply_is_not_fatal(monkeypatch, sqlstate, broken) -> None:
    """The server going away mid-apply (admin shutdown, connection failure, a
    broken connection) is an outage, not a rejected schema: it propagates as-is,
    boot carries on, and the next use retries."""
    from api.persistence.postgres import SqlConversationStore

    exc = _SqlStateError(sqlstate)
    _install_fake_pool(monkeypatch, _RaisingConn(exc, broken=broken))
    store = SqlConversationStore("postgresql://unused")

    with pytest.raises(_SqlStateError):
        store._ensure_pool()
    store.open()  # logged, not raised
    assert store._pool is None


def test_missing_driver_is_fatal_at_boot(monkeypatch) -> None:
    """The postgres backend without psycopg installed is a permanent
    misconfiguration, not a DB outage: open() must raise, not log 'not reachable'."""
    from api.persistence.postgres import SqlConversationStore

    monkeypatch.setitem(sys.modules, "psycopg_pool", None)  # import -> ImportError
    with pytest.raises(ImportError):
        SqlConversationStore("postgresql://unused").open()


class _LockTimeoutConn:
    """Times out (55P03) on the first ``failures`` DDL statements, like an ALTER
    queued behind an idle-in-transaction reader; records every statement."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.statements: list[str] = []
        self.params: list[object] = []

    def execute(self, sql: str, params: object = None) -> _NoRows:
        sql = " ".join(sql.split())
        self.statements.append(sql)
        self.params.append(params)
        if self.failures and sql.upper().startswith(("CREATE", "ALTER")):
            self.failures -= 1
            raise _SqlStateError("55P03")
        return _NoRows()


def test_boot_ddl_runs_under_a_lock_timeout_after_the_schema_lock(monkeypatch) -> None:
    """Unbounded, the boot ALTERs (ACCESS EXCLUSIVE even when the column exists) waited
    forever behind one idle-in-transaction reader, and every request on the table
    queued behind them (XERK-1603). The timeout is LOCAL, and set after the advisory
    lock so waiting on another replica's (bounded) apply isn't what times out."""
    from api.persistence.postgres import SCHEMA_LOCK_TIMEOUT_MS, SqlConversationStore

    conn = _LockTimeoutConn(failures=0)
    _install_fake_pool(monkeypatch, conn)
    SqlConversationStore("postgresql://unused")._ensure_pool()

    assert conn.statements[0].startswith("SELECT pg_advisory_xact_lock")
    assert conn.statements[1] == "SELECT set_config('lock_timeout', %s, true)"
    assert conn.params[1] == (f"{SCHEMA_LOCK_TIMEOUT_MS}ms",)


def test_boot_lock_timeout_is_retried(monkeypatch) -> None:
    """A lock timeout drops the whole apply (its transaction is aborted) and retries
    it from the advisory lock after a backoff, so a reader that ends meanwhile costs
    one short stall, not a failed boot."""
    import api.persistence.postgres as pg

    monkeypatch.setattr(pg, "SCHEMA_LOCK_BACKOFF_SECONDS", 0)
    conn = _LockTimeoutConn(failures=1)
    pools = _install_fake_pool(monkeypatch, conn)
    store = pg.SqlConversationStore("postgresql://unused")

    store.open()

    assert store._pool is pools[0]
    assert sum(s.startswith("SELECT pg_advisory_xact_lock") for s in conn.statements) == 2
    assert _creates_cues(conn.statements)


def test_boot_fails_when_a_table_lock_never_frees(monkeypatch) -> None:
    """Still blocked after every attempt: boot aborts as a schema it couldn't apply
    (the pod restarts and tries again) rather than serving on an unknown schema."""
    import api.persistence.postgres as pg

    monkeypatch.setattr(pg, "SCHEMA_LOCK_BACKOFF_SECONDS", 0)
    conn = _LockTimeoutConn(failures=10**6)
    pools = _install_fake_pool(monkeypatch, conn)
    store = pg.SqlConversationStore("postgresql://unused")

    with pytest.raises(pg.SchemaLockTimeout, match="could not take a table lock"):
        store.open()
    assert store._pool is None and pools[0].closed
    attempts = sum(s.startswith("SELECT pg_advisory_xact_lock") for s in conn.statements)
    assert attempts == pg.SCHEMA_LOCK_ATTEMPTS
