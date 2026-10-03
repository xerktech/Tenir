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
import types

import pytest

from api.persistence.postgres import (
    apply_schema,
    find_schema_file,
    iter_statements,
)


class _RecordingConn:
    """Captures the statements a schema-apply runs, normalized to single spaces."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(" ".join(sql.split()))


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
        def __init__(self, dsn: str, open: bool = True) -> None:  # noqa: A002 - psycopg kwarg
            self.dsn = dsn

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
        def __init__(self, dsn: str, open: bool = True) -> None:  # noqa: A002 - psycopg kwarg
            self.closed = False
            pools.append(self)

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
    /ready must report it — and each probe retries the pool + schema."""
    from fastapi.testclient import TestClient

    from api.main import app
    from api.persistence.postgres import SqlConversationStore

    attempts = []

    class _Down:
        def __init__(self, dsn: str, open: bool = True) -> None:  # noqa: A002
            attempts.append(self)

        def connection(self):
            raise TimeoutError("couldn't get a connection after 30 sec")

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
        assert resp.json()["checks"]["conversations"].startswith("error:")
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
        def execute(self, sql: str, params: object = None) -> None:
            nonlocal in_apply, max_in_apply
            with guard:
                in_apply += 1
                max_in_apply = max(max_in_apply, in_apply)
            time.sleep(0.001)
            with guard:
                in_apply -= 1

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
