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
import socket
import contextlib
import sys
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

from api.auth.sql_users import SqlUserStore
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
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, dsn: str, open: bool = True, **kw: object) -> None:  # noqa: A002
            assert open is False, "the pool must be opened with a bounded wait"
            assert kw["kwargs"]["connect_timeout"] == int(OPEN_TIMEOUT_SECONDS)
            # A blackholed network is bounded by TCP, not the kernel's ~15 minutes.
            assert kw["kwargs"]["keepalives"] == 1
            assert kw["kwargs"]["tcp_user_timeout"] > 0
            # The request path's wait for a connection is bounded too, not 30s.
            assert kw["timeout"] == OPEN_TIMEOUT_SECONDS
            # Stale connections from before a Postgres restart are checked, not lent.
            # ...by a check that can't hang on a server that stopped answering.
            assert kw["check"] is postgres.check_connection
            # A failed reconnect is retried fresh, not on a minute-long backoff.
            assert kw["reconnect_timeout"] == OPEN_TIMEOUT_SECONDS
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


@pytest.mark.parametrize("store_cls", [SqlConversationStore, SqlUserStore])
def test_unreachable_open_is_bounded_and_its_verdict_shared(monkeypatch, store_cls) -> None:
    """Both stores: the user store's 30s opens queued behind its lock on every
    authenticated request, stalling the shared thread pool (XERK-1434 QA)."""
    pools = _install_unreachable_pool(monkeypatch)
    store = store_cls("postgresql://unused")

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


def test_unreachable_verdict_is_shared_across_stores_of_one_dsn(monkeypatch) -> None:
    """XERK-1612: the user store opening right after the conversation store found the
    database unreachable gets that verdict, not a second full wait."""
    pools = _install_unreachable_pool(monkeypatch)
    with pytest.raises(_PoolTimeout):
        SqlConversationStore("postgresql://unused")._ensure_pool()
    with pytest.raises(DatabaseUnavailable, match="pool initialization incomplete"):
        SqlUserStore("postgresql://unused")._ensure_pool()
    assert len(pools) == 1
    # Another database's verdict is its own.
    with pytest.raises(_PoolTimeout):
        SqlUserStore("postgresql://other")._ensure_pool()
    assert len(pools) == 2


def test_lock_timeout_verdict_is_shared_across_openers_of_one_dsn(monkeypatch) -> None:
    """XERK-1612: a boot schema apply that timed out on a held table lock is shared by
    every store on that DSN for OPEN_TIMEOUT_SECONDS: each re-running the bounded
    apply (~10s) re-stalled requests on the locked table once per store. A rejected
    schema is not shared: it is retried on the very next call."""
    _install_base_pool(monkeypatch)
    applies: list[str] = []

    def lock_timeout(name: str):
        def init(pool) -> None:
            applies.append(name)
            raise postgres.SchemaLockTimeout("table lock held")

        return init

    conversations = postgres.PoolOpener("postgresql://unused", "conversations")
    users = postgres.PoolOpener("postgresql://unused", "users")
    with pytest.raises(postgres.SchemaLockTimeout, match="table lock held"):
        conversations.open(lock_timeout("conversations"))
    with pytest.raises(postgres.SchemaLockTimeout, match="shared verdict"):
        users.open(lock_timeout("users"))
    assert applies == ["conversations"]

    # Once the window passes the next open re-applies, so the stores recover.
    now = time.monotonic()
    monkeypatch.setattr(postgres.time, "monotonic", lambda: now + OPEN_TIMEOUT_SECONDS + 1)
    assert users.open(lambda pool: applies.append("users")) is not None
    assert applies == ["conversations", "users"]

    def rejected(pool) -> None:
        applies.append("rejected")
        raise postgres.SchemaApplyError("rejected")

    for _ in range(2):
        with pytest.raises(postgres.SchemaApplyError, match="rejected"):
            conversations.open(rejected)
    assert applies.count("rejected") == 2


def test_opens_of_one_dsn_wait_for_each_other(monkeypatch) -> None:
    """A store opening while the other store's apply is still running waits for its
    verdict instead of starting a second apply alongside it (XERK-1612)."""
    _install_base_pool(monkeypatch)
    started, release = threading.Event(), threading.Event()
    applies: list[str] = []

    def slow_lock_timeout(pool) -> None:
        applies.append("conversations")
        started.set()
        release.wait(5)
        raise postgres.SchemaLockTimeout("table lock held")

    errors: list[BaseException] = []

    def open_conversations() -> None:
        try:
            postgres.PoolOpener("postgresql://unused", "conversations").open(slow_lock_timeout)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    first = threading.Thread(target=open_conversations)
    first.start()
    assert started.wait(5)
    threading.Timer(0.2, release.set).start()
    with pytest.raises(postgres.SchemaLockTimeout, match="shared verdict"):
        postgres.PoolOpener("postgresql://unused", "users").open(
            lambda pool: applies.append("users")
        )
    first.join(5)
    assert applies == ["conversations"]
    assert len(errors) == 1 and isinstance(errors[0], postgres.SchemaLockTimeout)


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


def test_ws_token_resolution_runs_off_the_event_loop(monkeypatch) -> None:
    """Resolving a WS token reads the user store; on the event loop a blocking read
    (database down) froze every request server-wide, /health included."""
    from api import main

    on_loop: list[bool] = []

    def fake_principal(ws) -> None:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return None  # rejected -> the endpoint logs the reason and closes 1008

    monkeypatch.setattr(main, "_ws_principal", fake_principal)
    monkeypatch.setattr(main, "_ws_reject_reason", fake_principal)
    with TestClient(main.app) as client, client.websocket_connect("/ws?token=x") as ws:
        with pytest.raises(Exception):  # noqa: B017 - closed with 1008
            ws.receive_text()
    assert on_loop == [False, False]


# --- A database that stops answering after the pool opened (XERK-1513) ---------------


class _SocketConn:
    """A connection whose every statement blocks reading its socket, as against a
    server that stopped answering; a severed socket makes it fail like libpq does."""

    def __init__(self) -> None:
        self.sock, self.peer = socket.socketpair()
        self.autocommit = True

    def fileno(self) -> int:
        return self.sock.fileno()

    def execute(self, query: str) -> None:
        if not self.sock.recv(1):
            raise OSError("server closed the connection unexpectedly")


def _bounded(fn, seconds: float = 5.0):
    """Run ``fn`` in a daemon thread and fail — rather than hang the suite — if it is
    still blocked after ``seconds``: an unbounded wait is the very defect under test."""
    box: list = []

    def run() -> None:
        try:
            box.append(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            box.append(("raised", exc))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert box, f"still blocked after {seconds}s"
    kind, value = box[0]
    if kind == "raised":
        raise value
    return value


def _install_base_pool(monkeypatch) -> type:
    class _BasePool:
        def __init__(self, *a: object, configure=None, **kw: object) -> None:
            self.timeout = kw.get("timeout", 30.0)
            self.name = kw.get("name", "fake")
            self.configure = configure
            self.lent: list = []
            self.waits: list = []
            self.returned: list = []

        @staticmethod
        def check_connection(conn) -> None:
            conn.execute("")

        def getconn(self, timeout=None):
            self.waits.append(timeout)
            return self.lent.pop()

        def putconn(self, conn) -> None:
            # The watchdog must be disarmed before the pool can lend the
            # connection again, or it may sever the next borrower's query.
            assert id(conn) not in self._watchdogs
            self.returned.append(conn)

        def open(self, wait: bool = False, timeout: float = 30.0) -> None:
            pass

        def close(self) -> None:
            pass

        def reconnect_failed(self) -> None:
            pass

    fake_mod = types.ModuleType("psycopg_pool")
    fake_mod.ConnectionPool = _BasePool
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_mod)
    return postgres._guarded_pool_class()


def test_check_on_a_hung_server_is_bounded(monkeypatch) -> None:
    """psycopg's check runs execute("") with no limit: against a SIGSTOPped server
    each pooled connection trapped its borrower's thread indefinitely."""
    _install_base_pool(monkeypatch)
    monkeypatch.setattr(postgres, "CHECK_TIMEOUT_SECONDS", 0.2)
    conn = _SocketConn()
    t0 = time.monotonic()
    with pytest.raises(OSError, match="closed"):
        _bounded(lambda: postgres.check_connection(conn))
    assert time.monotonic() - t0 < 2


def test_healthy_check_and_borrow_leave_the_connection_alone(monkeypatch) -> None:
    pool_cls = _install_base_pool(monkeypatch)
    monkeypatch.setattr(postgres, "CHECK_TIMEOUT_SECONDS", 0.1)
    conn = _SocketConn()
    conn.peer.sendall(b"x")
    postgres.check_connection(conn)  # answered in time: disarmed

    pool = pool_cls(timeout=5.0)
    pool.borrow_timeout = 0.1
    pool.lent.append(conn)
    assert pool.getconn() is conn
    dog = pool._watchdogs[id(conn)]
    base = pool_cls.__bases__[0]
    original = base.putconn
    disarmed_first: list = []

    def putconn(self, c) -> None:
        # Disarmed before the pool can lend it again, or the timer may sever the
        # next borrower's query.
        disarmed_first.append(dog._done)
        original(self, c)

    monkeypatch.setattr(base, "putconn", putconn)
    pool.putconn(conn)
    assert disarmed_first == [True]
    time.sleep(0.3)  # past both bounds: neither watchdog may fire after its disarm
    conn.peer.sendall(b"y")
    assert conn.sock.recv(1) == b"y"
    assert pool.returned == [conn]


def test_a_borrow_on_a_hung_server_is_severed(monkeypatch) -> None:
    """An in-flight query has no client-side bound of its own; the borrow watchdog
    fails it instead of waiting for TCP to give up."""
    pool_cls = _install_base_pool(monkeypatch)
    pool = pool_cls(timeout=5.0)
    conn = _SocketConn()
    pool.lent.append(conn)
    assert pool.getconn() is conn  # no bound until the boot schema applied
    pool.putconn(conn)

    pool.borrow_timeout = 0.2
    pool.lent.append(conn)
    borrowed = pool.getconn()
    t0 = time.monotonic()
    with pytest.raises(OSError):
        _bounded(lambda: borrowed.execute("SELECT 1"))
    assert time.monotonic() - t0 < 2
    pool.putconn(borrowed)


def test_breaker_shortens_the_wait_only_while_the_database_is_unreachable(monkeypatch) -> None:
    """A burst against a dead database queued ceil(N/40) x 5s behind the worker
    limiter. Only a failed reconnect opens the breaker, never load; the next
    connection that opens closes it."""
    pool_cls = _install_base_pool(monkeypatch)
    pool = pool_cls(timeout=5.0)
    pool.lent += [object(), object(), object(), object()]

    pool.getconn()
    pool.getconn(timeout=OPEN_TIMEOUT_SECONDS)
    # A borrow just succeeded: a failed reconnect now means one slot can't be
    # refilled (max_connections), not an outage — the breaker stays closed.
    pool.reconnect_failed()
    assert not pool.unreachable

    now = time.monotonic()
    monkeypatch.setattr(postgres.time, "monotonic", lambda: now + OPEN_TIMEOUT_SECONDS + 1)
    pool.reconnect_failed()
    assert pool.unreachable
    pool.waits.clear()
    pool.lent += [object()]
    pool.getconn()  # a lent connection passed its check: the breaker closes
    assert pool.waits == [postgres.OUTAGE_WAIT_SECONDS]
    assert not pool.unreachable


def test_new_connections_get_the_statement_timeout_and_close_the_breaker(monkeypatch) -> None:
    pool_cls = _install_base_pool(monkeypatch)
    pool = pool_cls(timeout=5.0)
    pool.unreachable = True
    executed: list = []
    conn = types.SimpleNamespace(
        autocommit=False, execute=executed.append, commit=lambda: executed.append("COMMIT")
    )
    pool.configure(conn)
    assert executed == [
        f"SET statement_timeout = {int(postgres.STATEMENT_TIMEOUT_SECONDS * 1000)}",
        "COMMIT",
    ]
    assert postgres.STATEMENT_TIMEOUT_SECONDS < postgres.QUERY_TIMEOUT_SECONDS
    assert not pool.unreachable


def test_borrow_watchdog_is_armed_after_the_boot_schema_only(monkeypatch) -> None:
    """Boot DDL may run for minutes; every request-path borrow is bounded."""
    _install_base_pool(monkeypatch)
    seen: list = []
    pool = postgres.PoolOpener("postgresql://unused", "conversations").open(
        lambda p: seen.append(p.borrow_timeout)
    )
    assert seen == [None]
    assert pool.borrow_timeout == postgres.QUERY_TIMEOUT_SECONDS
    assert pool.name == "conversations"
