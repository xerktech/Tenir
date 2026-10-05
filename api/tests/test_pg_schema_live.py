"""schema.sql against a REAL Postgres (XERK-1406).

The recording-fake tests in test_pg_schema_apply.py prove the statements are split
and run on pool open, but cannot tell whether Postgres accepts them. XERK-651 added
``owner TEXT REFERENCES users(id)`` while ``users.id`` is UUID: Postgres refuses that
foreign key, the boot apply aborts, ``conversations.owner`` never exists and every
session.start fails (an ~11 h production outage). These tests apply the shipped
schema with the shipped ``apply_schema`` to a real database:

- a fresh database, twice (the boot apply runs on every pool open);
- several connections applying it at once to a fresh database, like api processes
  booting together (XERK-1509);
- a pre-XERK-651 data dir (no ``owner`` column), which must gain it and backfill
  legacy rows to the env admin.

Skipped unless ``TENIR_TEST_PG_DSN`` points at a disposable Postgres. CI provides
Postgres 18 + pgvector, like production, as a service container: api.yml on PRs, and
release.yml's schema-gate job on every release, which fails if these are skipped.
Each test works in its own throwaway schema, so the database itself is never
modified outside it.
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid

import pytest

from api.persistence.postgres import apply_schema, find_schema_file, lock_schema

DSN = os.environ.get("TENIR_TEST_PG_DSN", "")
psycopg = pytest.importorskip("psycopg") if DSN else None

pytestmark = pytest.mark.skipif(not DSN, reason="TENIR_TEST_PG_DSN not set (needs a real Postgres)")


@pytest.fixture
def conn():
    """An autocommit connection whose search_path is a fresh, dropped-after schema."""
    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
        c.execute(f"SET search_path TO {schema}, public")
        try:
            yield c
        finally:
            c.execute(f"DROP SCHEMA {schema} CASCADE")


def _schema_sql() -> str:
    path = find_schema_file()
    assert path is not None, "schema.sql not found"
    return path.read_text(encoding="utf-8")


def _columns(c, table: str) -> dict[str, str]:
    rows = c.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (table,),
    ).fetchall()
    return {name: dtype for name, dtype in rows}


def test_schema_applies_to_a_fresh_database_and_is_idempotent(conn) -> None:
    sql = _schema_sql()
    apply_schema(conn, sql)
    apply_schema(conn, sql)  # every pool open re-applies it
    assert _columns(conn, "conversations")["owner"] == "text"


def test_schema_upgrades_a_pre_ownership_data_dir_and_backfills(conn) -> None:
    sql = _schema_sql()
    apply_schema(conn, sql)
    # Recreate a pre-XERK-651 data dir: conversations without the owner column, one
    # legacy recording, and the env-managed admin the backfill attributes it to.
    conn.execute("ALTER TABLE conversations DROP COLUMN owner")
    conn.execute("INSERT INTO households (id) VALUES ('default') ON CONFLICT DO NOTHING")
    admin = conn.execute(
        "INSERT INTO users (household, username, role, is_env_admin) "
        "VALUES ('default', 'admin', 'admin', true) RETURNING id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO conversations (id, household, mic_source, started_at) "
        "VALUES ('legacy-1', 'default', 'phone-microphone', now())"
    )

    apply_schema(conn, sql)  # the boot apply on the upgraded image

    assert _columns(conn, "conversations")["owner"] == "text"
    owner = conn.execute("SELECT owner FROM conversations WHERE id = 'legacy-1'").fetchone()[0]
    # Stored as the admin's id rendered as text: what the code compares it against.
    assert owner == str(admin)


def test_concurrent_boot_applies_do_not_collide(conn) -> None:
    """Api processes booting together each apply the schema in their own transaction.
    Unguarded, racing CREATE ... IF NOT EXISTS failed one with 40P01 or a pg_type
    unique violation, which aborted its startup as a rejected schema (XERK-1509)."""
    sql = _schema_sql()
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    n = 4
    start = threading.Barrier(n)
    errors: list[BaseException] = []

    def boot() -> None:
        try:
            # Not autocommit, like a pooled connection: the xact lock lasts to commit.
            with psycopg.connect(DSN) as c:
                c.execute(f"SET search_path TO {schema}, public")
                c.commit()
                start.wait()
                lock_schema(c)
                apply_schema(c, sql)
        except BaseException as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    # Daemon + bounded join: a regression that deadlocks fails here instead of hanging CI.
    threads = [threading.Thread(target=boot, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not any(t.is_alive() for t in threads), "a schema apply never finished"
    assert errors == []
    assert _columns(conn, "conversations")["owner"] == "text"


def test_concurrent_first_use_of_both_stores_on_an_empty_database() -> None:
    """XERK-1430: the conversation and user stores each apply DDL on first use. On an
    empty database, running both at once raced — the users DDL before households
    existed (UndefinedTable), or two CREATE TABLE IF NOT EXISTS colliding
    (UniqueViolation on pg_type_typname_nsp_index). Both must succeed, every time."""
    import threading

    from api.auth.sql_users import SqlUserStore
    from api.persistence.postgres import SqlConversationStore

    for _ in range(5):
        schema = f"t_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {schema}")
        dsn = psycopg.conninfo.make_conninfo(DSN, options=f"-c search_path={schema},public")
        stores = [SqlUserStore(dsn), SqlConversationStore(dsn)]
        errors: list[BaseException] = []
        start = threading.Barrier(len(stores))

        def first_use(store) -> None:
            start.wait()
            try:
                store._ensure_pool()
            except BaseException as exc:  # noqa: BLE001 - collected for the assert
                errors.append(exc)

        threads = [threading.Thread(target=first_use, args=(s,)) for s in stores]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errors, errors
        finally:
            for store in stores:
                if store._pool is not None:
                    store._pool.close()
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(f"DROP SCHEMA {schema} CASCADE")


def test_boot_apply_does_not_stall_requests_behind_an_idle_reader(monkeypatch) -> None:
    """XERK-1603: one idle-in-transaction session that read ``conversations`` made the
    boot ALTER wait forever, and every request on the table queued behind it (>8s,
    until the reader ended). The apply must give up within its lock timeout, so a
    request stalls at most that long, and boot then fails instead of hanging."""
    import time

    import api.persistence.postgres as pg

    monkeypatch.setattr(pg, "SCHEMA_LOCK_TIMEOUT_MS", 300)
    monkeypatch.setattr(pg, "SCHEMA_LOCK_BACKOFF_SECONDS", 0.2)
    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    dsn = psycopg.conninfo.make_conninfo(DSN, options=f"-c search_path={schema},public")
    store = pg.SqlConversationStore(dsn)
    try:
        store.open()  # converge, so only the ALTER ... IF NOT EXISTS no-ops remain
        store._pool.close()
        store._pool = None
        with psycopg.connect(dsn) as idle, psycopg.connect(dsn, autocommit=True) as req:
            # Unfixed, the request blocks until the reader ends: fail, don't hang CI.
            req.execute("SET statement_timeout = '5s'")
            idle.execute("SELECT 1 FROM conversations")  # now idle in transaction
            errors: list[BaseException] = []

            def boot() -> None:
                try:
                    store.open()
                except BaseException as exc:  # noqa: BLE001 - asserted below
                    errors.append(exc)

            booting = threading.Thread(target=boot, daemon=True)
            booting.start()
            time.sleep(0.1)  # let the ALTER queue
            started = time.monotonic()
            req.execute("SELECT count(*) FROM conversations").fetchone()
            waited = time.monotonic() - started
            booting.join(timeout=30)

            assert not booting.is_alive(), "boot apply never gave up"
            assert waited < 2, f"request stalled {waited:.1f}s behind the boot DDL"
            assert len(errors) == 1 and isinstance(errors[0], pg.SchemaLockTimeout)
            assert store._pool is None

            # The reader ending lets the next attempt through.
            idle.rollback()
            store.open()
            assert store._pool is not None

            # The timeout is transaction-local: no pooled connection carries it into
            # request traffic, even after a failed attempt. Hold every connection at
            # once so each one is checked, not one reused four times.
            with contextlib.ExitStack() as stack:
                pooled = [
                    stack.enter_context(store._pool.connection())
                    for _ in range(store._pool.max_size)
                ]
                timeouts = {c.execute("SHOW lock_timeout").fetchone()[0] for c in pooled}
            assert timeouts == {"0"}
    finally:
        if store._pool is not None:
            store._pool.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
