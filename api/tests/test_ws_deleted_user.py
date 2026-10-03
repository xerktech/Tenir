"""XERK-1504: deleting a user must stop an already-open socket from recording.

Auth runs at the WS handshake, and ``DELETE /auth/users/{id}`` only revokes sessions
in the registry. A socket with no registered session at that moment — connected but
not yet started, or after ``session.end`` — was never closed, so it could
``session.start`` and record into the household after the account was removed.
"""

from __future__ import annotations

import json
import logging

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
            # The revoke ran inside the DELETE and must have closed ws2 (ws1 is gone,
            # so its hook is a no-op) — checked first so a regression fails here
            # rather than hanging on a receive from a socket nobody closes.
            assert caplog.text.count("ws closed: account no longer exists") == 1
            with pytest.raises(WebSocketDisconnect) as exc:
                ws2.receive_json()
            assert exc.value.code == 1008
        assert registry.get(sid) is None


@pytest.mark.real_auth
def test_delete_closes_both_sockets_when_a_resume_takes_over_an_open_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resume can take a session over while the socket it displaced is still open.
    The delete must close both, not just whichever socket bound it last."""
    _, admin_token = _token("admin", "admin")
    member_id, member_token = _token("member", "member")
    admin = {"Authorization": f"Bearer {admin_token}"}
    with TestClient(app) as client:
        with (
            client.websocket_connect(f"/ws?token={member_token}") as ws1,
            client.websocket_connect(f"/ws?token={member_token}") as ws2,
        ):
            ws1.send_text(START)
            sid = ws1.receive_json()["sessionId"]
            ws2.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "phone-microphone", "sessionId": sid}
                )
            )
            assert ws2.receive_json()["resumed"] is True
            assert client.delete(f"/auth/users/{member_id}", headers=admin).status_code == 204
            # The revoke ran inside the DELETE: both sockets were open, so both must
            # have been closed — checked here so a regression fails rather than hangs.
            assert caplog.text.count("ws closed: account no longer exists") == 2
            assert "could not close its socket" not in caplog.text
            for ws in (ws1, ws2):
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 1008


@pytest.mark.real_auth
def test_repeated_resumes_do_not_accumulate_revoke_hooks() -> None:
    """Each socket that binds a session registers a revoke hook; one that has gone
    away must drop it, or resuming one session over and over grows memory forever."""
    _, member_token = _token("member", "member")
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            ws.send_text(START)
            sid = ws.receive_json()["sessionId"]
        resume = json.dumps(
            {"type": "session.start", "micSource": "phone-microphone", "sessionId": sid}
        )
        for _ in range(5):
            with client.websocket_connect(f"/ws?token={member_token}") as ws:
                # Twice on one socket: re-resuming its own session must not stack hooks.
                for _ in range(2):
                    ws.send_text(resume)
                    assert ws.receive_json()["resumed"] is True
                assert len(registry.get(sid)._disconnects) == 1
        assert registry.get(sid)._disconnects == []


@pytest.mark.real_auth
def test_revoke_during_resume_replay_still_closes_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoke landing while rebind() replays runs before the resuming socket has
    registered its hook, so the resume must notice and close the socket itself."""
    from api import session as session_mod

    member_id, member_token = _token("member", "member")
    real_rebind = session_mod.Session.rebind

    async def rebind_then_revoke(self, send):
        await real_rebind(self, send)
        get_user_store().delete(member_id)
        registry.unregister(self)
        await self.revoke("account deleted")  # what the DELETE does, mid-resume

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            ws.send_text(START)
            sid = ws.receive_json()["sessionId"]
        monkeypatch.setattr(session_mod.Session, "rebind", rebind_then_revoke)
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            ws.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "phone-microphone", "sessionId": sid}
                )
            )
            # Without the check this is session.ready resumed=True on a live socket.
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008


@pytest.mark.real_auth
def test_message_after_revoke_close_is_a_quiet_disconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """XERK-1517: a frame the handler reads after a revoke has closed the socket
    must end the handler like a disconnect. Replying to it raises starlette's
    send-after-close RuntimeError, which escaped as "Exception in ASGI application"."""
    caplog.set_level(logging.INFO, logger="api")
    _, admin_token = _token("admin", "admin")
    member_id, member_token = _token("member", "member")
    admin = {"Authorization": f"Bearer {admin_token}"}

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            ws.send_text(START)
            assert ws.receive_json()["type"] == "session.ready"
            assert client.delete(f"/auth/users/{member_id}", headers=admin).status_code == 204
            # Still queued for the handler after the server's close: its pong can't be sent.
            ws.send_text(json.dumps({"type": "ping", "t": 1}))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008
        # Leaving the block re-raises anything that escaped the handler.
    assert "client disconnected" in caplog.text


@pytest.mark.real_auth
def test_runtime_error_on_open_socket_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a RuntimeError on a closed socket is a disconnect; any other is a bug and
    must not be swallowed."""
    from api import main

    def boom(text: str):
        raise RuntimeError("real bug")

    monkeypatch.setattr(main, "parse_client_message", boom)
    _, member_token = _token("member", "member")
    with TestClient(app) as client:
        # No receive: if the error were swallowed, a receive would wait forever on a
        # socket nobody closes. Leaving the block re-raises what the handler raised.
        with pytest.raises(RuntimeError, match="real bug"):
            with client.websocket_connect(f"/ws?token={member_token}") as ws:
                ws.send_text(START)
