"""A database that dies or hangs after the pools opened, against a REAL Postgres (XERK-1513).

Three failures QA found once the pool was warm:

- A hung-but-ACKing server (SIGSTOP) trapped one worker thread per pooled connection
  indefinitely: psycopg's connection check and in-flight queries had no client-side bound.
- After an outage the first request succeeded up to a minute after the database was back:
  each broken connection retried on psycopg's exponential backoff (1, 2, 4 ... 64s).
- A burst against a dead database queued behind the worker limiter, every request
  waiting out the full connection timeout.

The store reaches Postgres through an in-test TCP proxy that can refuse connections (an
outage) or freeze traffic while keeping sockets open (a hung server). Skipped unless
``TENIR_TEST_PG_DSN`` points at a disposable Postgres (CI provides one).
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from api.persistence import postgres

DSN = os.environ.get("TENIR_TEST_PG_DSN", "")
psycopg = pytest.importorskip("psycopg") if DSN else None

pytestmark = pytest.mark.skipif(not DSN, reason="TENIR_TEST_PG_DSN not set (needs a real Postgres)")


class _Proxy:
    """Forwards TCP to the test Postgres. ``down()`` closes every connection and refuses
    new ones; ``freeze()`` stops forwarding bytes but keeps sockets open, as a stopped
    server whose kernel still ACKs."""

    def __init__(self, target: tuple) -> None:
        self._target = target
        self._frozen = threading.Event()
        self._conns: list[socket.socket] = []
        self._listener: socket.socket | None = None
        self.port = 0
        self.up()

    def _connect_target(self) -> socket.socket:
        if isinstance(self._target, str):  # a Unix socket path
            s = socket.socket(socket.AF_UNIX)
            s.connect(self._target)
            return s
        return socket.create_connection(self._target)

    def up(self) -> None:
        lsock = socket.socket()
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lsock.bind(("127.0.0.1", self.port))
        lsock.listen(64)
        self.port = lsock.getsockname()[1]
        self._listener = lsock
        threading.Thread(target=self._accept, args=(lsock,), daemon=True).start()

    def down(self) -> None:
        if self._listener is not None:
            # shutdown, not just close: a close leaves the accept() blocked in
            # _accept still listening on Linux.
            _close(self._listener)
            self._listener = None
        for s in self._conns:
            _close(s)
        self._conns.clear()

    def freeze(self) -> None:
        self._frozen.set()

    def thaw(self) -> None:
        self._frozen.clear()

    def _accept(self, lsock: socket.socket) -> None:
        while True:
            try:
                client, _ = lsock.accept()
            except OSError:
                return
            try:
                server = self._connect_target()
            except OSError:
                _close(client)
                continue
            self._conns += [client, server]
            for src, dst in ((client, server), (server, client)):
                threading.Thread(target=self._pump, args=(src, dst), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                while self._frozen.is_set():
                    time.sleep(0.05)
                dst.sendall(data)
        except OSError:
            pass
        _close(src)
        _close(dst)


def _close(s: socket.socket) -> None:
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    s.close()


@pytest.fixture
def rig():
    """(store, proxy): a conversation store in a throwaway schema, reached via the proxy."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from api.persistence.postgres import SqlConversationStore

    info = conninfo_to_dict(DSN)
    host = str(info.get("host") or "localhost")
    port = int(info.get("port") or 5432)
    target = f"{host}/.s.PGSQL.{port}" if host.startswith("/") else (host, port)
    proxy = _Proxy(target)
    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        store = SqlConversationStore(
            make_conninfo(
                DSN, host="127.0.0.1", port=proxy.port, options=f"-c search_path={schema}"
            )
        )
        try:
            store._ensure_pool()
            for _ in range(8):  # warm every pooled connection
                store.list("h")
            yield store, proxy
        finally:
            proxy.thaw()
            proxy.down()
            if store._pool is not None:
                store._pool.close(timeout=1)
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _timed(fn) -> tuple[str, float]:
    t0 = time.monotonic()
    try:
        fn()
        outcome = "ok"
    except Exception as exc:  # noqa: BLE001 - classified below
        outcome = "outage" if postgres.is_database_unavailable(exc) else repr(exc)
    return outcome, time.monotonic() - t0


def _wait_for(cond, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_hung_server_bounds_every_request(rig) -> None:
    """More requests than pooled connections against a frozen server all fail as an
    outage within the connection wait plus the check bound — none hangs."""
    store, proxy = rig
    proxy.freeze()
    bound = postgres.OPEN_TIMEOUT_SECONDS + postgres.CHECK_TIMEOUT_SECONDS + 2
    with ThreadPoolExecutor(10) as ex:
        futures = [ex.submit(_timed, lambda: store.list("h")) for _ in range(10)]
        results = [f.result(timeout=bound + 5) for f in futures]
    assert {r for r, _ in results} == {"outage"}, results
    assert max(d for _, d in results) < bound, results

    proxy.thaw()
    assert _wait_for(lambda: _timed(lambda: store.list("h"))[0] == "ok", 10)


def test_in_flight_query_on_a_hung_server_is_severed(rig, monkeypatch) -> None:
    """A query already running when the server stops answering fails after the borrow
    bound as an outage (503), instead of waiting for TCP to give up."""
    store, proxy = rig
    pool = store._pool
    monkeypatch.setattr(pool, "borrow_timeout", 1.0)

    def query() -> None:
        with pool.connection() as conn:
            proxy.freeze()
            conn.execute("SELECT 1")

    outcome, took = _timed(query)
    assert outcome == "outage"
    assert took < 3

    proxy.thaw()
    assert _wait_for(lambda: _timed(lambda: store.list("h"))[0] == "ok", 10)


def test_outage_fails_fast_then_recovers_promptly(rig) -> None:
    """Once the pool gives up reconnecting, requests fail in OUTAGE_WAIT_SECONDS rather
    than the full wait; when the database is back the next request succeeds."""
    store, proxy = rig
    proxy.down()
    # Requests during the outage drive the pool's reconnects until it gives up.
    assert _wait_for(
        lambda: _timed(lambda: store.list("h")) and store._pool.unreachable,
        postgres.OPEN_TIMEOUT_SECONDS * 4,
    )

    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(lambda _: _timed(lambda: store.list("h")), range(30)))
    assert {r for r, _ in results} == {"outage"}, results
    assert max(d for _, d in results) < postgres.OUTAGE_WAIT_SECONDS + 1, results

    # Back after a long outage: psycopg's backoff would have waited up to a minute.
    time.sleep(2)
    proxy.up()
    t0 = time.monotonic()
    assert _wait_for(lambda: _timed(lambda: store.list("h"))[0] == "ok", 5)
    assert time.monotonic() - t0 < 5
    assert not store._pool.unreachable
