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
            with pytest.raises(WebSocketDisconnect) as exc:
                while True:
                    ws.receive_json()  # session.ready may arrive before the close
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
