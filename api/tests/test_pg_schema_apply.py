"""Regression: the api must self-heal an existing Postgres data dir on boot.

Postgres only runs ``schema.sql`` on a FRESH volume (docker-entrypoint-initdb.d),
so a database created before an additive change never gets it. The cue work
(XERK-81) added a ``cues`` table that ``SqlConversationStore.get`` reads
unconditionally; ``create`` (the session.start path) calls ``get``. On an
upgraded-in-place database the ``cues`` table was missing, so every ``get`` — and
thus every ``session.start`` — raised ``relation "cues" does not exist``, which
surfaced to clients as ``could not start session`` and killed transcription
entirely.

The fix applies the idempotent schema on connection-pool open. psycopg isn't
installed in CI (the SQL backend is an extra) and the pooled paths need a live
database, so these tests exercise the real splitting/apply logic and the pool
wiring against a recording fake — no driver, no database.
"""

from __future__ import annotations

import contextlib
import sys
import types

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
    # Idempotent guard: every statement is a safe CREATE/INSERT so a converged DB
    # re-applies cleanly.
    assert all(
        "IF NOT EXISTS" in s or "ON CONFLICT" in s or s.upper().startswith("INSERT")
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
