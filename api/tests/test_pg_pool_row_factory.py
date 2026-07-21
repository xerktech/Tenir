"""Regression: SQL stores must not poison a *pooled* connection's row factory.

The status dashboard reported the Postgres connection "going in and out". Root
cause: several store methods set ``conn.row_factory = dict_row`` on a connection
borrowed from a shared ``psycopg_pool`` pool. psycopg's pool resets transaction
state on return but *not* ``row_factory``, so the mutation sticks for the life of
that physical connection. A later tuple-indexing reader sharing the pool
(``SqlConversationStore.households`` — the ``GET /status`` probe — or
``SqlKnowledgeStore.documents``) then does ``r[0]`` on a dict row and raises
``KeyError(0)``. Because the pool hands out clean vs poisoned connections at
random, the probe flapped green/yellow.

psycopg isn't installed in CI (the SQL backends are ``pip install -e '.[…]'``
extras), and the stores import ``dict_row`` lazily inside each method. So these
tests stub ``psycopg.rows`` in ``sys.modules`` and inject a fake pool whose
connection is reused as-is across checkouts (row factory not reset) — exercising
the real method logic with no driver and no database. The invariant: after any
read, the pooled connection's row factory is unchanged, and a tuple-indexing
reader still works.
"""

from __future__ import annotations

import contextlib
import sys
import types

import pytest

# Sentinel standing in for the pool's default (tuple) row factory.
_DEFAULT = object()


@pytest.fixture
def dict_row():
    """Stub ``psycopg.rows.dict_row`` so the stores' lazy import resolves without
    the real driver. Returns the sentinel the stores will pass as a row factory."""
    sentinel = object()
    rows_mod = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = sentinel
    saved_pkg = sys.modules.get("psycopg")
    saved_rows = sys.modules.get("psycopg.rows")
    sys.modules.setdefault("psycopg", types.ModuleType("psycopg"))
    sys.modules["psycopg.rows"] = rows_mod
    try:
        yield sentinel
    finally:
        if saved_rows is not None:
            sys.modules["psycopg.rows"] = saved_rows
        else:
            sys.modules.pop("psycopg.rows", None)
        if saved_pkg is not None:
            sys.modules["psycopg"] = saved_pkg
        elif "psycopg" in sys.modules and saved_rows is None:
            sys.modules.pop("psycopg", None)


class _FakeCursor:
    def __init__(self, factory: object, dict_row: object) -> None:
        self._factory = factory
        self._dict_row = dict_row
        self._cols: list[str] = []
        self._rows: list[list] = []

    def execute(self, sql: str, params: object = None) -> "_FakeCursor":
        text = " ".join(sql.split())
        if "DISTINCT household" in text:  # SqlConversationStore.households()
            self._cols, self._rows = ["household"], [["default"]]
        elif "GROUP BY ref" in text:  # SqlKnowledgeStore.documents()
            self._cols, self._rows = ["ref", "title", "chunks"], [["r1", "Doc", 3]]
        else:  # reads that build models from a (here empty) result set
            self._cols, self._rows = ["id"], []
        return self

    def _shape(self, vals: list) -> object:
        # A dict_row connection yields mappings; the default yields tuples. That
        # is exactly the difference that makes r[0] raise KeyError(0) on a
        # connection poisoned to dict_row.
        if self._factory is self._dict_row:
            return dict(zip(self._cols, vals))
        return tuple(vals)

    def fetchall(self) -> list:
        return [self._shape(v) for v in self._rows]

    def fetchone(self) -> object:
        return self._shape(self._rows[0]) if self._rows else None


class _FakeConn:
    """A pooled connection whose row_factory persists across checkouts."""

    def __init__(self, dict_row: object) -> None:
        self._dict_row = dict_row
        self.row_factory: object = _DEFAULT

    def execute(self, sql: str, params: object = None) -> _FakeCursor:
        return _FakeCursor(self.row_factory, self._dict_row).execute(sql, params)

    def cursor(self, *, row_factory: object = None) -> _FakeCursor:
        factory = row_factory if row_factory is not None else self.row_factory
        return _FakeCursor(factory, self._dict_row)

    @contextlib.contextmanager
    def transaction(self):
        yield


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @contextlib.contextmanager
    def connection(self):
        # psycopg's pool reuses the connection as-is — it does NOT reset
        # row_factory between checkouts.
        yield self._conn


def test_conversation_reads_do_not_poison_pool_then_households_works(dict_row) -> None:
    from api.persistence.postgres import SqlConversationStore

    conn = _FakeConn(dict_row)
    store = SqlConversationStore("postgresql://unused")
    store._pool = _FakePool(conn)

    # A dict_row read runs first, exactly as the app does when listing history.
    assert store.list("default") == []

    # Invariant: the pooled connection comes back with its default row factory.
    assert conn.row_factory is _DEFAULT

    # And the GET /status probe's households() reads tuple rows without KeyError.
    assert store.households() == ["default"]


def test_conversation_search_does_not_poison_pool(dict_row) -> None:
    from api.persistence.postgres import SqlConversationStore

    conn = _FakeConn(dict_row)
    store = SqlConversationStore("postgresql://unused")
    store._pool = _FakePool(conn)

    assert store.search("default", "anything") == []
    assert conn.row_factory is _DEFAULT
    assert store.households() == ["default"]

