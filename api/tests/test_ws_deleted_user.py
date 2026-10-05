"""XERK-1504: deleting a user must stop an already-open socket from recording.

Auth runs at the WS handshake, and ``DELETE /auth/users/{id}`` only revokes sessions
in the registry. A socket with no registered session at that moment — connected but
not yet started, or after ``session.end`` — was never closed, so it could
``session.start`` and record into the household after the account was removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from api import registry
from api.auth import Principal, get_user_store, issue_token, reset_user_store
from api.main import WS_CLOSE_RESUMED_ELSEWHERE, app
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
def test_delete_closes_the_socket_a_resume_took_over_from_an_open_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resume can take a session over while the socket it displaced is still open.
    The takeover closes the displaced one (XERK-1526), and the delete must then close
    the socket that took it over — not only the one the session was started on."""
    caplog.set_level(logging.INFO, logger="api")
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
            # Synchronous first, so a takeover that never closes ws1 fails, not hangs.
            assert caplog.text.count("ws closed: session resumed on another socket") == 1
            with pytest.raises(WebSocketDisconnect) as exc:
                ws1.receive_json()
            assert exc.value.code == WS_CLOSE_RESUMED_ELSEWHERE
            assert client.delete(f"/auth/users/{member_id}", headers=admin).status_code == 204
            # The revoke ran inside the DELETE: checked here so a regression fails
            # rather than hangs.
            assert caplog.text.count("ws closed: account no longer exists") == 1
            assert "could not close its socket" not in caplog.text
            with pytest.raises(WebSocketDisconnect) as exc:
                ws2.receive_json()
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


def test_audio_after_revoke_is_not_recorded() -> None:
    """XERK-1525: revoke() finalizes the session before it closes the socket, and the
    handler keeps reading binary frames until the close lands — they never trigger a
    send, so nothing ends the loop. PCM arriving in that window reached on_audio and
    was stored (and transcribed) with the recording of a deleted account."""
    import asyncio

    from api.contract import ServerMessage
    from api.persistence import get_audio_store, get_conversation_store
    from api.persistence.audio import audio_key
    from api.persistence.wav import wav_to_pcm16
    from api.session import Session
    from api.stt.stub import StubTranscriber

    gate = asyncio.Event()
    pushed: list[bytes] = []

    class SlowFlush(StubTranscriber):
        async def push(self, pcm: bytes) -> None:
            pushed.append(pcm)
            await super().push(pcm)

        async def flush(self) -> None:
            await gate.wait()

    async def send(_msg: ServerMessage) -> None:
        return None

    async def run() -> None:
        session = Session(send)
        session._transcriber = SlowFlush()
        session._pump = asyncio.create_task(session._pump_results())
        get_conversation_store().create(session._household, session.session_id)
        before, after = b"\x01\x00" * 160, b"\x7f\x00" * 160
        await session.on_audio(before)
        revoke = asyncio.create_task(session.revoke("account deleted"))
        await asyncio.sleep(0.05)  # teardown is parked in the STT flush
        await session.on_audio(after)  # a frame the handler read after the revoke
        gate.set()
        await asyncio.wait_for(revoke, timeout=5)
        await session.on_audio(after)  # and one after the revoke returned
        stored = get_audio_store().get(audio_key(session._household, session.session_id))
        assert stored is not None
        assert wav_to_pcm16(stored) == before
        assert pushed == [before]

    asyncio.run(run())


@pytest.mark.real_auth
def test_delete_does_not_wait_on_the_revoked_sockets_close_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (XERK-1550): on uvicorn's legacy websockets backend ws.close() waits
    for the peer's close frame, so a frozen client held the admin's DELETE for the
    20 s close timeout. The patched close stands in for that peer: it doesn't finish
    until the test releases it (or 5 s pass), so a DELETE awaiting it is slow."""
    _, admin_token = _token("admin", "admin")
    member_id, member_token = _token("member", "member")
    admin = {"Authorization": f"Bearer {admin_token}"}
    released = threading.Event()
    real_close = WebSocket.close

    async def frozen_peer_close(self: WebSocket, code: int = 1000, reason: str | None = None):
        if code == 1008:
            for _ in range(50):
                if released.is_set():
                    break
                await asyncio.sleep(0.1)
        await real_close(self, code=code, reason=reason)

    monkeypatch.setattr(WebSocket, "close", frozen_peer_close)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            ws.send_text(START)
            assert ws.receive_json()["type"] == "session.ready"
            t0 = time.monotonic()
            assert client.delete(f"/auth/users/{member_id}", headers=admin).status_code == 204
            assert time.monotonic() - t0 < 2
            released.set()
            # The close still reaches the client once its peer answers.
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008
    assert not [s for s in registry.active() if s.user_id == member_id]


def _slow_close(monkeypatch: pytest.MonkeyPatch, slow_code: int) -> None:
    """Delay closes with ``slow_code`` so frames sent meanwhile queue up ahead of them."""
    real_close = WebSocket.close

    async def slow_close(self: WebSocket, code: int = 1000, reason: str | None = None) -> None:
        if code == slow_code:
            await asyncio.sleep(0.3)
        await real_close(self, code=code, reason=reason)

    monkeypatch.setattr(WebSocket, "close", slow_close)


@pytest.mark.real_auth
def test_revoked_socket_gets_its_1008_even_with_a_frame_queued() -> None:
    """XERK-1550, QA: the 1008 goes out in a background task, and on uvicorn's legacy
    backend its send blocks until the peer answers. A frame queued meanwhile wakes the
    handler, whose reply to the now-closing socket ends it; returning then let uvicorn
    drop the transport and the 1008 with it, so the client saw 1006. TestClient
    delivers a close sent after the app returns, so watch the ASGI order directly."""
    _, admin_token = _token("admin", "admin")
    member_id, member_token = _token("member", "member")
    events: list[str] = []

    async def observed(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        async def watch(message) -> None:  # type: ignore[no-untyped-def]
            if message.get("code") == 1008:
                await asyncio.sleep(0.3)  # the legacy backend awaiting the peer
                events.append("1008 sent")
            await send(message)

        if scope["type"] != "websocket":
            return await app(scope, receive, send)
        await app(scope, receive, watch)
        events.append("handler returned")

    ping = json.dumps({"type": "ping", "t": 1})
    with TestClient(observed) as client:
        with client.websocket_connect(f"/ws?token={member_token}") as ws:
            ws.send_text(START)
            assert ws.receive_json()["type"] == "session.ready"
            headers = {"Authorization": f"Bearer {admin_token}"}
            assert client.delete(f"/auth/users/{member_id}", headers=headers).status_code == 204
            with pytest.raises(WebSocketDisconnect) as exc:
                for _ in range(1000):  # until one ping lands while the close is going out
                    ws.send_text(ping)
                    ws.receive_json()
            assert exc.value.code == 1008
            assert events == ["1008 sent", "handler returned"]


@pytest.mark.real_auth
def test_delete_leaves_a_socket_already_closing_as_displaced_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """XERK-1550, QA: a socket gets one close. A delete landing while the displaced
    socket's 4001 is still going out must not schedule a 1008 over it — only the
    socket that took the session over counts as removed."""
    caplog.set_level(logging.INFO, logger="api")
    _, admin_token = _token("admin", "admin")
    member_id, member_token = _token("member", "member")
    _slow_close(monkeypatch, WS_CLOSE_RESUMED_ELSEWHERE)
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
            headers = {"Authorization": f"Bearer {admin_token}"}
            assert client.delete(f"/auth/users/{member_id}", headers=headers).status_code == 204
            assert caplog.text.count("ws closed: account no longer exists") == 1
            with pytest.raises(WebSocketDisconnect) as exc:
                ws1.receive_json()
            assert exc.value.code == WS_CLOSE_RESUMED_ELSEWHERE
            with pytest.raises(WebSocketDisconnect) as exc:
                ws2.receive_json()
            assert exc.value.code == 1008


@pytest.mark.real_auth
def test_resume_does_not_wait_on_the_displaced_sockets_close_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XERK-1550, QA: the displacement's 4001 shares the background close. Awaited
    inline, a frozen displaced peer held the resume (and its session.ready) for the
    legacy backend's 20 s close timeout. The patched close stands in for that peer."""
    _, member_token = _token("member", "member")
    released = threading.Event()
    real_close = WebSocket.close

    async def frozen_peer_close(self: WebSocket, code: int = 1000, reason: str | None = None):
        if code == WS_CLOSE_RESUMED_ELSEWHERE:
            for _ in range(50):
                if released.is_set():
                    break
                await asyncio.sleep(0.1)
        await real_close(self, code=code, reason=reason)

    monkeypatch.setattr(WebSocket, "close", frozen_peer_close)
    with TestClient(app) as client:
        with (
            client.websocket_connect(f"/ws?token={member_token}") as ws1,
            client.websocket_connect(f"/ws?token={member_token}") as ws2,
        ):
            ws1.send_text(START)
            sid = ws1.receive_json()["sessionId"]
            t0 = time.monotonic()
            ws2.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "phone-microphone", "sessionId": sid}
                )
            )
            assert ws2.receive_json()["resumed"] is True
            assert time.monotonic() - t0 < 2
            released.set()
            with pytest.raises(WebSocketDisconnect) as exc:
                ws1.receive_json()
            assert exc.value.code == WS_CLOSE_RESUMED_ELSEWHERE
