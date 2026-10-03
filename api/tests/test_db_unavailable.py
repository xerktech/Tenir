"""XERK-1510: a database outage is a 503 (retryable), not a 500 + traceback.

With Postgres down, the token-liveness lookup and the stores raise
``DatabaseUnavailable`` / psycopg_pool's ``PoolTimeout`` / a connection-level
``OperationalError``. Unhandled, each request was a 500 with a full traceback and
uvicorn then dropped the connection. The REST contract is now 503 + ``Retry-After``
logged as one WARNING line; the WS handshake accepts and closes 1013 (try again
later), which clients reconnect on — never the 1008 that means "re-login".
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from api import history, main
from api.auth import Principal, get_user_store, issue_token, reset_user_store
from api.main import DB_UNAVAILABLE_DETAIL, app
from api.persistence.postgres import (
    DatabaseUnavailable,
    database_error_types,
    is_database_unavailable,
)
from conftest import TEST_AUTH_SECRET


def _raise(exc: BaseException):
    def boom(*_a, **_kw):
        raise exc

    return boom


def _member_token() -> tuple[str, str]:
    reset_user_store()
    user = get_user_store().create("member", "pw", household="hh", role="member")
    token = issue_token(
        Principal(user.user_id, "hh", "member"), secret=TEST_AUTH_SECRET, ttl_seconds=60
    )
    return user.user_id, token


@pytest.mark.real_auth
def test_liveness_lookup_outage_is_503_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, token = _member_token()
    monkeypatch.setattr(get_user_store(), "get_by_id", _raise(DatabaseUnavailable("refused")))

    with TestClient(app, raise_server_exceptions=False) as client, caplog.at_level(logging.WARNING):
        r = client.get("/conversations", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 503
    assert r.json() == {"detail": DB_UNAVAILABLE_DETAIL}
    assert r.headers["retry-after"] == "5"
    outage = [rec for rec in caplog.records if "database unavailable" in rec.getMessage()]
    assert len(outage) == 1
    assert outage[0].levelno == logging.WARNING
    assert outage[0].exc_info is None  # one line, no traceback
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    reset_user_store()


def test_store_outage_on_a_route_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    class DownStore:
        def __getattr__(self, _name: str):
            return _raise(DatabaseUnavailable("refused"))

    monkeypatch.setattr(history, "get_conversation_store", lambda: DownStore())
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/conversations")
    assert r.status_code == 503
    assert r.json()["detail"] == DB_UNAVAILABLE_DETAIL


@pytest.mark.real_auth
def test_renewal_lookup_outage_keeps_the_routes_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sliding-renewal lookup runs after the route; the database going away
    between the two must cost only the renewal, not turn a 200 into a 500."""
    _, token = _member_token()
    store = get_user_store()
    real_get = store.get_by_id
    calls = {"n": 0}

    def flaky(user_id: str):
        calls["n"] += 1
        if calls["n"] > 1:
            raise DatabaseUnavailable("refused")
        return real_get(user_id)

    monkeypatch.setattr(store, "get_by_id", flaky)
    monkeypatch.setattr(main, "renew_token_if_due", lambda tok, **_kw: tok)  # renewal always due
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert main.RENEWED_TOKEN_HEADER.lower() not in r.headers
    assert calls["n"] == 2
    reset_user_store()


def test_ws_handshake_outage_closes_1013(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_ws_principal", _raise(DatabaseUnavailable("refused")))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == 1013


def test_ws_outage_after_accept_closes_1013(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Starlette runs app exception handlers for websocket routes too: an outage
    escaping the open-socket loop (e.g. persisting on session.end) must close 1013,
    not crash the handler on ``request.method`` and drop the socket as a 1006."""
    monkeypatch.setattr(main, "parse_client_message", _raise(DatabaseUnavailable("refused")))
    with TestClient(app) as client, caplog.at_level(logging.WARNING):
        with client.websocket_connect("/ws") as ws:
            ws.send_text('{"type": "session.end"}')
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
    assert exc.value.code == 1013
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]


@pytest.mark.parametrize("state", [WebSocketState.CONNECTING, WebSocketState.CONNECTED])
@pytest.mark.parametrize("gone", [WebSocketDisconnect(1006), RuntimeError("closed")])
def test_ws_outage_close_to_a_departed_client_is_quiet(state, gone: Exception) -> None:
    """The client may leave while the server waits out the pool; the socket state
    still says connected (starlette only learns of it on a receive), so the 1013
    close itself raises. That must not escape as an ASGI crash."""

    async def receive() -> dict:
        return {"type": "websocket.disconnect", "code": 1006}

    async def send(_msg: dict) -> None:
        raise gone

    ws = WebSocket({"type": "websocket", "path": "/ws", "headers": []}, receive, send)
    ws.application_state = state
    ws.client_state = WebSocketState.CONNECTED
    asyncio.run(main._database_unavailable(ws, DatabaseUnavailable("refused")))


def test_ws_session_start_account_check_outage_logs_one_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(get_user_store(), "get_by_id", _raise(DatabaseUnavailable("refused")))
    with TestClient(app) as client, caplog.at_level(logging.WARNING):
        with client.websocket_connect("/ws") as ws:
            ws.send_text('{"type": "session.start", "micSource": "phone-microphone"}')
            assert ws.receive_json()["code"] == "internal"
    failed = [rec for rec in caplog.records if "account check failed" in rec.getMessage()]
    assert len(failed) == 1
    assert failed[0].levelno == logging.WARNING
    assert failed[0].exc_info is None


@pytest.mark.real_auth
def test_renewal_skipped_after_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """A route that already 503'd must not wait out the pool a second time for renewal."""
    _, token = _member_token()
    store = get_user_store()
    calls = {"n": 0}

    def down(_user_id: str):
        calls["n"] += 1
        raise DatabaseUnavailable("refused")

    monkeypatch.setattr(store, "get_by_id", down)
    monkeypatch.setattr(main, "renew_token_if_due", lambda tok, **_kw: tok)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    assert calls["n"] == 1
    reset_user_store()


def test_outage_log_keeps_first_line_only() -> None:
    exc = DatabaseUnavailable("terminating connection\nLINE 1: SELECT secret FROM users")
    assert main._outage_summary(exc) == "DatabaseUnavailable: terminating connection"
    assert main._outage_summary(DatabaseUnavailable()) == "DatabaseUnavailable: "


def test_ws_handshake_other_error_is_not_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_ws_principal", _raise(ValueError("bug")))
    with TestClient(app) as client, pytest.raises(ValueError, match="bug"):
        with client.websocket_connect("/ws"):
            pass


def test_handler_reraises_a_real_fault() -> None:
    """An exception class registered for 503 that is NOT an outage (an
    OperationalError like disk full) must stay a 500, so it is re-raised."""
    err = RuntimeError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        asyncio.run(main._database_unavailable(None, err))  # type: ignore[arg-type]


# --- classification -----------------------------------------------------------


@pytest.fixture
def fake_psycopg(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Stand-in psycopg/psycopg_pool modules: CI installs neither (the persistence
    extra is optional), and the classification only needs their exception classes."""

    class OperationalError(Exception):
        def __init__(self, msg: str, sqlstate: str | None = None) -> None:
            super().__init__(msg)
            self.sqlstate = sqlstate

    class PoolTimeout(Exception):
        pass

    psycopg = types.ModuleType("psycopg")
    psycopg.OperationalError = OperationalError  # type: ignore[attr-defined]
    pool = types.ModuleType("psycopg_pool")
    pool.PoolTimeout = PoolTimeout  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool)
    return types.SimpleNamespace(OperationalError=OperationalError, PoolTimeout=PoolTimeout)


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (lambda m: DatabaseUnavailable("x"), True),
        (lambda m: m.PoolTimeout("no connection in 5s"), True),
        # The server never answered (refused / closed unexpectedly): no SQLSTATE.
        (lambda m: m.OperationalError("connection refused"), True),
        (lambda m: m.OperationalError("connection failure", "08006"), True),
        (lambda m: m.OperationalError("admin shutdown", "57P01"), True),
        # Real faults that happen to be OperationalErrors stay 500s.
        (lambda m: m.OperationalError("disk full", "53100"), False),
        (lambda m: m.OperationalError("index row too large", "54000"), False),
        (lambda m: ValueError("bug"), False),
    ],
    ids=["unavailable", "pool-timeout", "no-sqlstate", "08", "57P01", "53100", "54000", "other"],
)
def test_is_database_unavailable(fake_psycopg, make, expected: bool) -> None:
    assert is_database_unavailable(make(fake_psycopg)) is expected


def test_database_error_types_with_psycopg(fake_psycopg) -> None:
    assert database_error_types() == (
        DatabaseUnavailable,
        fake_psycopg.PoolTimeout,
        fake_psycopg.OperationalError,
    )


def test_without_psycopg_only_database_unavailable_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)  # import raises ImportError
    assert database_error_types() == (DatabaseUnavailable,)
    assert is_database_unavailable(DatabaseUnavailable("x"))
    assert not is_database_unavailable(ValueError("x"))
