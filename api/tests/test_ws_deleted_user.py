"""XERK-1504: deleting a user must stop an already-open socket from recording.

Auth runs at the WS handshake, and ``DELETE /auth/users/{id}`` only revokes sessions
in the registry. A socket with no registered session at that moment — connected but
not yet started, or after ``session.end`` — was never closed, so it could
``session.start`` and record into the household after the account was removed.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api import registry
from api.auth import Principal, get_user_store, issue_token, reset_user_store
from api.main import app
from conftest import TEST_AUTH_SECRET

START = json.dumps({"type": "session.start", "micSource": "phone-microphone"})
END = json.dumps({"type": "session.end"})


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_user_store()
    for s in registry.active():
        registry.unregister(s)
    yield
    reset_user_store()
    for s in registry.active():
        registry.unregister(s)


def _token(username: str, role: str) -> tuple[str, str]:
    user = get_user_store().create(username, "pw", household="hh", role=role)
    token = issue_token(
        Principal(user.user_id, "hh", role), secret=TEST_AUTH_SECRET, ttl_seconds=60
    )
    return user.user_id, token


@pytest.mark.real_auth
@pytest.mark.parametrize("ended_first", [False, True], ids=["idle", "after-end"])
def test_deleted_user_socket_cannot_session_start(ended_first: bool) -> None:
    _, admin_token = _token("admin", "admin")
    member_id, member_token = _token("member", "member")
    admin = {"Authorization": f"Bearer {admin_token}"}

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            if ended_first:
                ws.send_text(START)
                assert ws.receive_json()["type"] == "session.ready"
                ws.send_text(END)
                # Round-trip a ping so session.end has been processed before the delete.
                ws.send_text(json.dumps({"type": "ping", "t": 1}))
                while ws.receive_json()["type"] != "pong":
                    pass
            else:
                ws.send_text(json.dumps({"type": "ping", "t": 1}))
                assert ws.receive_json()["type"] == "pong"

            assert client.delete(f"/auth/users/{member_id}", headers=admin).status_code == 204

            ws.send_text(START)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008
        assert not [s for s in registry.active() if s.user_id == member_id]


@pytest.mark.real_auth
def test_live_user_socket_can_restart_after_end() -> None:
    """The liveness re-check must not get in the way of a still-existing user."""
    _, member_token = _token("member", "member")
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            for _ in range(2):
                ws.send_text(START)
                assert ws.receive_json()["type"] == "session.ready"
                ws.send_text(END)


@pytest.mark.real_auth
def test_delete_during_session_start_revokes_the_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delete landing while start() is awaiting scans the registry before the new
    session is in it; the post-register re-check must still revoke it."""
    from api import session as session_mod

    member_id, member_token = _token("member", "member")
    real_start = session_mod.Session.start

    async def start_then_delete(self, **kwargs):
        await real_start(self, **kwargs)
        get_user_store().delete(member_id)  # the delete's registry scan finds nothing

    monkeypatch.setattr(session_mod.Session, "start", start_then_delete)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            ws.send_text(START)
            # start() sends session.ready before the post-register re-check runs.
            assert ws.receive_json()["type"] == "session.ready"
            # Without the re-check this ping is answered; with it, the close comes first.
            ws.send_text(json.dumps({"type": "ping", "t": 1}))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008
    assert not [s for s in registry.active() if s.user_id == member_id]


@pytest.mark.real_auth
def test_account_check_error_sends_error_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store outage at session.start fails closed with an error frame, socket kept."""
    from api import main

    _, member_token = _token("member", "member")

    async def boom(user_id: str) -> bool:
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "_account_exists", boom)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            ws.send_text(START)
            msg = ws.receive_json()
            assert msg["type"] == "error" and msg["code"] == "internal"
            ws.send_text(json.dumps({"type": "ping", "t": 2}))
            assert ws.receive_json() == {"type": "pong", "t": 2}


@pytest.mark.real_auth
def test_resumed_socket_is_closed_when_its_account_is_deleted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delete's revoke closed the socket the session was STARTED on, so a socket
    that had resumed it stayed open after the account was gone."""
    _, admin_token = _token("admin", "admin")
    member_id, member_token = _token("member", "member")
    admin = {"Authorization": f"Bearer {admin_token}"}
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws1:
            ws1.send_text(START)
            sid = ws1.receive_json()["sessionId"]
        # ws1 dropped without session.end: the session waits in the grace window.
        with client.websocket_connect(f"/ws?token={member_token}") as ws2:
            ws2.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "phone-microphone", "sessionId": sid}
                )
            )
            assert ws2.receive_json()["resumed"] is True
            assert client.delete(f"/auth/users/{member_id}", headers=admin).status_code == 204
            # The revoke ran inside the DELETE. Had it aimed at the dead ws1 it logs
            # this — check it first so a regression fails here rather than hanging
            # on a receive from a socket nobody closes.
            assert "could not close its socket" not in caplog.text
            with pytest.raises(WebSocketDisconnect) as exc:
                ws2.receive_json()
            assert exc.value.code == 1008
        assert registry.get(sid) is None


@pytest.mark.real_auth
def test_start_on_socket_whose_session_outlived_the_delete_closes_1008() -> None:
    """If the delete's registry scan missed this socket's session, session.start
    finalizes it and still closes the socket with 1008, not a bare drop."""
    member_id, member_token = _token("member", "member")
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            ws.send_text(START)
            sid = ws.receive_json()["sessionId"]
            live = registry.get(sid)
            registry.unregister(live)  # the delete's scan won't find it
            get_user_store().delete(member_id)
            ws.send_text(START)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008
            assert live.is_closed
