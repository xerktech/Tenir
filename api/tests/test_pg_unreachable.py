"""Regression (XERK-1434): an unreachable Postgres must not stall boot or /ready.

Every call made while the database was down built a fresh pool and waited
psycopg's 30s default for a connection, serialized behind the store's pool lock:
boot took ~2 minutes (open, the readiness probe, the stale sweep and the status
probe each waited in turn) and each unauthenticated GET /ready held a worker
thread 30-60s. These tests pin the bounded open, the shared failure verdict, the
bounded readiness probe and the single-flight /ready — against fake pools, no
database.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
import types

import pytest

from api.persistence import postgres
from api.persistence.postgres import (
    OPEN_TIMEOUT_SECONDS,
    DatabaseUnavailable,
    SqlConversationStore,
)


class _PoolTimeout(Exception):
    pass


def _install_unreachable_pool(monkeypatch) -> list:
    """A psycopg_pool whose open(wait=True) times out, as on an unreachable DB."""
    pools: list = []

    class _FakePool:
        def __init__(self, dsn: str, open: bool = True, kwargs: dict | None = None) -> None:  # noqa: A002
            assert open is False, "the pool must be opened with a bounded wait"
            assert kwargs == {"connect_timeout": int(OPEN_TIMEOUT_SECONDS)}
            self.open_timeout: float | None = None
            self.closed = False
            pools.append(self)

        def open(self, wait: bool = False, timeout: float = 30.0) -> None:
            assert wait, "open must wait so an unreachable DB is detected here"
            self.open_timeout = timeout
            raise _PoolTimeout(f"pool initialization incomplete after {timeout} sec")

        def close(self) -> None:
            self.closed = True

    fake_mod = types.ModuleType("psycopg_pool")
    fake_mod.ConnectionPool = _FakePool
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_mod)
    return pools


def test_unreachable_open_is_bounded_and_its_verdict_shared(monkeypatch) -> None:
    pools = _install_unreachable_pool(monkeypatch)
    store = SqlConversationStore("postgresql://unused")

    with pytest.raises(_PoolTimeout):
        store._ensure_pool()
    assert pools[0].open_timeout == OPEN_TIMEOUT_SECONDS
    assert pools[0].closed, "a pool that never opened must be closed"

    # A caller right behind it (the next boot step, a queued /ready) gets the
    # verdict immediately instead of paying another full wait.
    with pytest.raises(DatabaseUnavailable, match="pool initialization incomplete"):
        store._ensure_pool()
    assert len(pools) == 1

    # Once the window passes the next call retries, so the store recovers.
    now = time.monotonic()
    monkeypatch.setattr(postgres.time, "monotonic", lambda: now + OPEN_TIMEOUT_SECONDS + 1)
    with pytest.raises(_PoolTimeout):
        store._ensure_pool()
    assert len(pools) == 2
    assert store._pool is None


def test_open_and_ready_fail_fast_on_unreachable_db(monkeypatch) -> None:
    """The boot sequence: open() logs the outage, the readiness probe that
    follows fails without another wait."""
    pools = _install_unreachable_pool(monkeypatch)
    store = SqlConversationStore("postgresql://unused")

    store.open()  # logged, not raised
    with pytest.raises(DatabaseUnavailable):
        store.ready()
    assert len(pools) == 1


def test_ready_bounds_the_connection_wait() -> None:
    """With the pool open but the DB gone, ready() must not wait the pool's
    request-path timeout."""
    seen: dict[str, object] = {}

    class _Conn:
        def execute(self, sql: str) -> None:
            seen["sql"] = sql

    class _OpenPool:
        @contextlib.contextmanager
        def connection(self, timeout: float | None = None):
            seen["timeout"] = timeout
            yield _Conn()

    store = SqlConversationStore("postgresql://unused")
    store._pool = _OpenPool()
    store.ready()
    assert seen == {"timeout": OPEN_TIMEOUT_SECONDS, "sql": "SELECT 1"}


def test_concurrent_ready_requests_share_one_probe(monkeypatch) -> None:
    """A burst of the public /ready holds one worker thread, not one per request."""
    from api import main

    calls = 0
    lock = threading.Lock()

    def slow_probe() -> dict[str, str]:
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.2)
        return {"conversations": "error"}

    monkeypatch.setattr(main, "probe_backends", slow_probe)
    monkeypatch.setattr(main, "_ready_probe", None)

    async def burst() -> list:
        return await asyncio.gather(*(main.ready() for _ in range(20)))

    responses = asyncio.run(burst())
    assert calls == 1
    assert {r.status_code for r in responses} == {503}

    # A later request probes afresh rather than serving the stale result forever.
    asyncio.run(burst())
    assert calls == 2


def test_ready_ignores_a_probe_pending_on_another_loop(monkeypatch) -> None:
    """A probe future left pending by a closed loop must not be awaited from a new one."""
    from api import main

    stale_loop = asyncio.new_event_loop()
    monkeypatch.setattr(main, "_ready_probe", stale_loop.create_future())  # never resolves
    monkeypatch.setattr(main, "probe_backends", lambda: {"conversations": "ok"})
    try:
        assert asyncio.run(main.ready()).status_code == 200
    finally:
        stale_loop.close()
