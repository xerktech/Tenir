"""Reconnect-with-resume.

A dropped socket keeps its session (and transcriber state) alive for a grace
window so a reconnect carrying the same id rebinds to it instead of starting a
fresh one.
"""

from __future__ import annotations

import asyncio
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from api import main, registry
from api.auth import Principal, get_user_store
from api.contract import Pong, ServerMessage
from api.main import WS_CLOSE_RESUMED_ELSEWHERE, app, settings
from api.metrics import metrics
from api.persistence import audio_key, get_audio_store, get_conversation_store, wav_to_pcm16
from api.session import Session


@pytest.fixture(autouse=True)
def _reset() -> None:
    get_conversation_store()._by_household.clear()
    get_audio_store()._blobs.clear()
    for s in registry.active():
        registry.unregister(s)
    yield
    for s in registry.active():
        registry.unregister(s)


def _voice_chunk(freq: int = 200, *, ms: int = 100, amp: int = 8000) -> bytes:
    n = 16000 * ms // 1000
    t = np.arange(n) / 16000.0
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16).tobytes()


def test_detach_then_rebind_cancels_grace_and_reroutes_sends() -> None:
    async def run() -> None:
        first: list[ServerMessage] = []
        second: list[ServerMessage] = []

        async def send1(msg: ServerMessage) -> None:
            first.append(msg)

        async def send2(msg: ServerMessage) -> None:
            second.append(msg)

        session = Session(send1)
        await session.start(
            mic_source="phone-microphone", source_lang=None
        )

        session.detach(grace_seconds=30)
        assert session._detached and not session.is_closed
        assert session._grace_task is not None

        await session.rebind(send2)
        assert session.resumed is True
        assert session.current_send is send2
        assert session._grace_task is None  # grace cancelled by the resume

        # Subsequent sends now reach the reconnected socket, not the dead one.
        # (`first` already holds the session.ready from start(); the pong must not.)
        await session.current_send(Pong(type="pong", t=1))
        assert second and second[0].type == "pong"
        assert all(m.type != "pong" for m in first)

        await session.close()

    asyncio.run(run())


def test_messages_during_grace_window_are_buffered_and_replayed_on_resume() -> None:
    """A drop must not silently lose captions: messages produced while detached
    are buffered and replayed, in order, to the resumed socket."""

    async def run() -> None:
        second: list[ServerMessage] = []

        async def send1(_msg: ServerMessage) -> None:
            pass

        async def send2(msg: ServerMessage) -> None:
            second.append(msg)

        session = Session(send1)
        await session.start(mic_source="phone-microphone", source_lang=None)

        session.detach(grace_seconds=30)
        # Work finishing during the gap is buffered, not delivered or dropped.
        await session.current_send(Pong(type="pong", t=1))
        await session.current_send(Pong(type="pong", t=2))
        assert second == []

        await session.rebind(send2)
        # Replayed to the reconnected socket, in order.
        assert [m.t for m in second if m.type == "pong"] == [1, 2]

        await session.close()

    asyncio.run(run())


def test_grace_window_expiry_finalizes_and_unregisters() -> None:
    async def run() -> None:
        async def send(_msg: ServerMessage) -> None:
            pass

        session = Session(send, session_id="grace-1")
        await session.start(
            mic_source="g2-microphone", source_lang=None
        )
        registry.register(session)

        session.detach(grace_seconds=0)  # 0 -> finalize on the next loop turn
        for _ in range(5):
            await asyncio.sleep(0)
        assert session.is_closed
        assert registry.get("grace-1") is None

    asyncio.run(run())


def test_stale_grace_close_does_not_evict_a_live_session_with_the_same_id() -> None:
    """Regression (XERK-1507): two Sessions can share an id (racing cold resumes),
    the later one registered over the earlier. The stale one's grace close must
    not unregister the live one, or it drops out of /health, resume and shutdown."""

    async def run() -> None:
        async def send(_msg: ServerMessage) -> None:
            pass

        stale = Session(send, session_id="dup-1")
        await stale.start(mic_source="g2-microphone", source_lang=None)
        registry.register(stale)
        live = Session(send, session_id="dup-1")
        await live.start(mic_source="g2-microphone", source_lang=None)
        registry.register(live)

        stale.detach(grace_seconds=0)
        for _ in range(5):
            await asyncio.sleep(0)
        assert stale.is_closed
        assert registry.get("dup-1") is live

        registry.unregister(live)
        assert registry.get("dup-1") is None
        await live.close()

    asyncio.run(run())


def test_detach_after_close_is_noop() -> None:
    async def run() -> None:
        async def send(_msg: ServerMessage) -> None:
            pass

        session = Session(send)
        await session.start(
            mic_source="g2-microphone", source_lang=None
        )
        await session.close()
        session.detach(grace_seconds=30)  # closed already -> no grace task
        assert session._grace_task is None

    asyncio.run(run())


def test_resume_keeps_the_same_stt_state() -> None:
    """The whole point of resume: the transcriber survives the drop, instead of
    being rebuilt from scratch — which would reset the rolling buffer/VAD state."""

    async def run() -> None:
        async def send(_msg: ServerMessage) -> None:
            pass

        session = Session(send, session_id="resume-1")
        await session.start(
            mic_source="phone-microphone", source_lang=None
        )
        for _ in range(10):  # feed some voiced audio so state is non-trivial
            await session.on_audio(_voice_chunk())
        transcriber_before = session._transcriber

        async def send2(_msg: ServerMessage) -> None:
            pass

        session.detach(grace_seconds=30)
        await session.rebind(send2)

        # Same object -> the rolling buffer and VAD state are carried across the
        # reconnect (the bug rebuilt a fresh Session per connect).
        assert session._transcriber is transcriber_before

        await session.close()

    asyncio.run(run())


def test_ws_reconnect_resumes_live_session() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "session.start", "micSource": "phone-microphone"}))
            sid = ws.receive_json()["sessionId"]

        # Dropped without an explicit session.end -> kept alive for resume.
        live = registry.get(sid)
        assert live is not None

        with client.websocket_connect("/ws") as ws2:
            ws2.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "phone-microphone", "sessionId": sid}
                )
            )
            ready = ws2.receive_json()
            assert ready["type"] == "session.ready"
            assert ready["sessionId"] == sid
            assert ready["resumed"] is True
            # The very same Session object was rebound (not a fresh one with the
            # same id), so transcriber continuity is genuinely preserved.
            assert registry.get(sid) is live


def test_ws_warm_resume_repeats_a_delayed_caption_status_after_ready() -> None:
    """Clients clear "captions delayed" on every session.ready, because a cold resume
    starts a fresh, undelayed session that never says so (XERK-1498). A warm resume
    of a session still delayed must therefore repeat it after its ready."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "session.start", "micSource": "phone-microphone"}))
            sid = ws.receive_json()["sessionId"]
        live = registry.get(sid)
        assert live is not None
        live._captions_delayed = True

        with client.websocket_connect("/ws") as ws2:
            ws2.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "phone-microphone", "sessionId": sid}
                )
            )
            assert ws2.receive_json()["type"] == "session.ready"
            assert ws2.receive_json() == {"type": "caption.status", "delayed": True}


def test_ws_resume_after_finalize_extends_retained_audio() -> None:
    """The glasses persist their session id and reconnect with it long after the
    grace window has lapsed — by then the first leg has been finalized and
    unregistered, so the reconnect starts a *brand-new* Session on the same
    conversation id. Its audio must extend the stored clip, not replace it, so the
    web UI can replay the whole glasses session rather than just its last leg
    (XERK-86)."""

    def audio_samples(sid: str) -> int:
        wav = get_audio_store().get(audio_key("default", sid))
        return len(wav_to_pcm16(wav)) // 2 if wav else 0

    def drain_to_pong(ws) -> None:
        # Voiced audio yields caption frames; skip past them to the pong that
        # confirms the preceding session.end has been fully handled.
        while ws.receive_json()["type"] != "pong":
            pass

    with TestClient(app) as client:
        # Leg 1: capture audio, then end explicitly. session.end finalizes and
        # unregisters the session (persisting its audio) — the same terminal state a
        # dropped session reaches once its grace window expires.
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "session.start", "micSource": "g2-microphone"}))
            sid = ws.receive_json()["sessionId"]
            for _ in range(20):
                ws.send_bytes(_voice_chunk(freq=200))
            ws.send_text(json.dumps({"type": "session.end"}))
            # A ping after the end round-trips through the handler, so session.end
            # (and its synchronous persist) is guaranteed done before we read back.
            ws.send_text(json.dumps({"type": "ping", "t": 1}))
            drain_to_pong(ws)

        assert registry.get(sid) is None, "leg 1 must be finalized before the resume"
        first = audio_samples(sid)
        assert first > 0

        # Leg 2: reconnect carrying the finalized id -> a fresh Session bound to the
        # same conversation, capturing more audio.
        with client.websocket_connect("/ws") as ws2:
            ws2.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "g2-microphone", "sessionId": sid}
                )
            )
            assert ws2.receive_json()["sessionId"] == sid
            for _ in range(20):
                ws2.send_bytes(_voice_chunk(freq=400))
            ws2.send_text(json.dumps({"type": "session.end"}))
            ws2.send_text(json.dumps({"type": "ping", "t": 2}))
            drain_to_pong(ws2)

        # The retained clip spans both legs, not just the most recent one.
        assert audio_samples(sid) > first


def test_racing_cold_resumes_of_one_id_share_a_single_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (XERK-1514): two reconnects carrying the same finalized id that
    land inside one cold start's start() window must not each build their own
    Session — the second has to resume onto the first, or the duplicate writes
    the same conversation unseen by /health, warm resume, revoke and shutdown."""
    started: list[Session] = []
    real_start = Session.start

    async def slow_start(self: Session, **kwargs: object) -> None:
        started.append(self)
        await asyncio.sleep(0.3)  # widen the check-then-register window
        await real_start(self, **kwargs)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "session.start", "micSource": "g2-microphone"}))
            sid = ws.receive_json()["sessionId"]
            ws.send_text(json.dumps({"type": "session.end"}))
            ws.send_text(json.dumps({"type": "ping", "t": 1}))
            assert ws.receive_json()["type"] == "pong"
        assert registry.get(sid) is None

        monkeypatch.setattr(Session, "start", slow_start)
        start = json.dumps(
            {"type": "session.start", "micSource": "g2-microphone", "sessionId": sid}
        )
        with client.websocket_connect("/ws") as b, client.websocket_connect("/ws") as c:
            b.send_text(start)
            c.send_text(start)
            ready = [b.receive_json(), c.receive_json()]
            assert [r["sessionId"] for r in ready] == [sid, sid]
            assert len(started) == 1, "both reconnects cold-started their own Session"
            # The one Session is the registered one, so both sockets are bound to
            # something /health, warm resume, revoke and shutdown can all see.
            assert registry.get(sid) is started[0]
            assert registry.count() == 1


def test_warm_resume_closes_the_socket_it_takes_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (XERK-1526): resuming a session that is still bound to an OPEN
    socket used to leave that socket open and silent. Once the new socket dropped
    and the grace close finalized the session, the old one kept streaming audio
    into a closed Session with no captions and no close. It must instead get a
    distinct close code its client does not auto-reconnect on."""
    monkeypatch.setattr(settings, "session_resume_grace_seconds", 0.2)
    start = {"type": "session.start", "micSource": "g2-microphone"}
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as b:
            b.send_text(json.dumps(start))
            sid = b.receive_json()["sessionId"]
            live = registry.get(sid)
            displaced = metrics.snapshot()["counters"].get("ws.displaced", 0)
            with client.websocket_connect("/ws") as c:
                c.send_text(json.dumps({**start, "sessionId": sid}))
                assert c.receive_json()["resumed"] is True
                # The takeover closes the old socket before it sends session.ready:
                # checked synchronously so a regression fails instead of hanging below.
                assert metrics.snapshot()["counters"].get("ws.displaced", 0) == displaced + 1
                with pytest.raises(WebSocketDisconnect) as closed:
                    b.receive_json()
                assert closed.value.code == WS_CLOSE_RESUMED_ELSEWHERE
                # The displaced socket can no longer end the session it lost: the
                # takeover stops its handler before it can process another frame.
                b.send_text(json.dumps({"type": "session.end"}))
                c.send_text(json.dumps({"type": "ping", "t": 1}))
                assert c.receive_json()["type"] == "pong"
                assert registry.get(sid) is live and not live.is_closed
            # The new socket dropping still parks the session for its own resume.
            time.sleep(0.05)
            assert registry.get(sid) is live


@pytest.mark.parametrize("restart_with_id", [False, True], ids=["fresh", "same-id"])
def test_a_start_in_flight_on_the_displaced_socket_leaves_the_session_alone(
    monkeypatch: pytest.MonkeyPatch, restart_with_id: bool
) -> None:
    """Regression (XERK-1526, QA): a socket displaced while its own session.start is
    awaiting (here the account check) must not finish that start. A fresh start
    would close the session the new socket now owns; a same-id one would displace
    the new socket in turn and rebind the session to the closed old one."""
    real_check = main._account_exists
    slow: list[bool] = []

    async def account_exists(user_id: str) -> bool:
        if slow:
            slow.clear()
            await asyncio.sleep(0.5)
        return await real_check(user_id)

    monkeypatch.setattr(main, "_account_exists", account_exists)
    start = {"type": "session.start", "micSource": "g2-microphone"}
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            a.send_text(json.dumps(start))
            sid = a.receive_json()["sessionId"]
            live = registry.get(sid)
            slow.append(True)  # A's next start stalls in its account check
            a.send_text(json.dumps({**start, "sessionId": sid} if restart_with_id else start))
            time.sleep(0.15)
            b.send_text(json.dumps({**start, "sessionId": sid}))
            assert b.receive_json()["resumed"] is True
            with pytest.raises(WebSocketDisconnect) as closed:
                a.receive_json()
            assert closed.value.code == WS_CLOSE_RESUMED_ELSEWHERE
            time.sleep(0.6)  # past A's stalled check
            b.send_text(json.dumps({"type": "ping", "t": 1}))
            assert b.receive_json()["type"] == "pong"
            assert registry.get(sid) is live and not live.is_closed
            assert live.current_send is not None and registry.count() == 1


def test_a_cold_resume_in_flight_on_the_displaced_socket_leaves_the_session_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (XERK-1526, QA): displaced while its cold resume of an ended
    recording awaits the owner-check store read, a socket must not go on to close
    the live session it was bound to — the one the new socket now owns."""
    convs = get_conversation_store()
    real_get = convs.get
    slow: list[bool] = []

    def get(*args: object, **kwargs: object):
        if slow:
            slow.clear()
            time.sleep(0.5)
        return real_get(*args, **kwargs)

    start = {"type": "session.start", "micSource": "g2-microphone"}
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps(start))
            ended = ws.receive_json()["sessionId"]
            ws.send_text(json.dumps({"type": "session.end"}))
            ws.send_text(json.dumps({"type": "ping", "t": 1}))
            assert ws.receive_json()["type"] == "pong"
        monkeypatch.setattr(convs, "get", get)
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            a.send_text(json.dumps(start))
            sid = a.receive_json()["sessionId"]
            live = registry.get(sid)
            slow.append(True)  # A's cold resume stalls in its owner-check read
            a.send_text(json.dumps({**start, "sessionId": ended}))
            time.sleep(0.15)
            b.send_text(json.dumps({**start, "sessionId": sid}))
            assert b.receive_json()["resumed"] is True
            with pytest.raises(WebSocketDisconnect) as closed:
                a.receive_json()
            assert closed.value.code == WS_CLOSE_RESUMED_ELSEWHERE
            time.sleep(0.6)  # past A's stalled read
            b.send_text(json.dumps({"type": "ping", "t": 2}))
            assert b.receive_json()["type"] == "pong"
            assert registry.get(sid) is live and not live.is_closed
            assert registry.get(ended) is None


def test_the_displaced_socket_gets_its_4001_even_with_a_frame_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (XERK-1526, QA): the 4001 goes out in a background task. A frame
    already queued on the displaced socket wakes its handler first; returning then
    let uvicorn drop the transport and the 4001 with it, so the client saw 1006,
    reconnected with the same id and displaced the new socket. TestClient delivers
    a close sent after the app returns, so watch the ASGI order directly."""
    real_close = WebSocket.close

    async def slow_close(self: WebSocket, code: int = 1000, reason: str | None = None) -> None:
        if code == WS_CLOSE_RESUMED_ELSEWHERE:
            await asyncio.sleep(0.3)  # let the queued frame reach the handler first
        await real_close(self, code=code, reason=reason)

    events: list[str] = []

    async def observed(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        async def watch(message) -> None:  # type: ignore[no-untyped-def]
            if message.get("code") == WS_CLOSE_RESUMED_ELSEWHERE:
                events.append("4001 sent")
            await send(message)

        if scope["type"] != "websocket":
            return await app(scope, receive, send)
        await app(scope, receive, watch)
        events.append("handler returned")

    monkeypatch.setattr(WebSocket, "close", slow_close)
    start = {"type": "session.start", "micSource": "g2-microphone"}
    with TestClient(observed) as client:
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            a.send_text(json.dumps(start))
            sid = a.receive_json()["sessionId"]
            b.send_text(json.dumps({**start, "sessionId": sid}))
            assert b.receive_json()["resumed"] is True
            a.send_text(json.dumps({"type": "ping", "t": 1}))  # queued behind the takeover
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    a.receive_json()
            assert closed.value.code == WS_CLOSE_RESUMED_ELSEWHERE
            # A's handler has returned once its close is out; B's is still running.
            assert events == ["4001 sent", "handler returned"]


def test_resuming_onto_the_same_socket_does_not_close_it() -> None:
    start = {"type": "session.start", "micSource": "g2-microphone"}
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps(start))
        sid = ws.receive_json()["sessionId"]
        ws.send_text(json.dumps({**start, "sessionId": sid}))
        assert ws.receive_json()["resumed"] is True
        ws.send_text(json.dumps({"type": "ping", "t": 1}))
        assert ws.receive_json()["type"] == "pong"


def test_a_resume_target_ending_mid_takeover_cold_resumes_instead_of_revoking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (XERK-1597): a warm resume awaits closing the socket's own old
    session before it rebinds. If the target ended meanwhile, the handler rebound
    onto a closed session and read that as a revoked account: a valid user's socket
    got 1008 "account removed", which the client treats as an auth failure."""
    real_close = Session.close
    slow: list[Session] = []

    async def slow_close(self: Session) -> None:
        if self in slow:
            slow.remove(self)
            await asyncio.sleep(0.5)
        await real_close(self)

    monkeypatch.setattr(Session, "close", slow_close)
    start = {"type": "session.start", "micSource": "g2-microphone"}
    removed = metrics.snapshot()["counters"].get("ws.account_removed", 0)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            a.send_text(json.dumps(start))
            old = registry.get(a.receive_json()["sessionId"])
            b.send_text(json.dumps(start))
            sid = b.receive_json()["sessionId"]
            target = registry.get(sid)
            slow.append(old)  # A's takeover stalls closing its own session
            a.send_text(json.dumps({**start, "sessionId": sid}))
            time.sleep(0.15)
            b.send_text(json.dumps({"type": "session.end"}))  # target ends meanwhile
            b.send_text(json.dumps({"type": "ping", "t": 1}))
            assert b.receive_json()["type"] == "pong"
            assert target.is_closed
            # A reopens the finalized recording instead of being told its account is gone.
            ready = a.receive_json()
            assert ready["type"] == "session.ready" and ready["sessionId"] == sid
            a.send_text(json.dumps({"type": "ping", "t": 2}))
            assert a.receive_json()["type"] == "pong"
            resumed = registry.get(sid)
            assert resumed is not None and resumed is not target and not resumed.is_closed
            assert old.is_closed and registry.get(old.session_id) is None
    assert metrics.snapshot()["counters"].get("ws.account_removed", 0) == removed


def _as_household(monkeypatch: pytest.MonkeyPatch, household: str) -> None:
    """Route the next WS connection's principal to a real admin of ``household``."""
    store = get_user_store()
    user = store.get_by_username(household) or store.create(
        household, "pw", household=household, role="admin"
    )
    monkeypatch.setattr(
        main,
        "_ws_principal",
        lambda ws: Principal(
            user_id=user.user_id, username=household, household=household, role="admin"
        ),
    )


def test_a_foreign_id_start_does_not_hold_the_owner_off_its_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (XERK-1526): a start presenting another household's live id runs
    under a fresh server id, so it must not hold that id's start lock for its whole
    start() — anyone knowing the UUID could delay the owner's resume with it."""
    real_start = Session.start

    async def slow_foreign_start(self: Session, **kwargs: object) -> None:
        if self.household == "intruder":
            await asyncio.sleep(0.6)
        await real_start(self, **kwargs)

    start = {"type": "session.start", "micSource": "g2-microphone"}
    with TestClient(app) as client:
        _as_household(monkeypatch, "owner")
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps(start))
            sid = ws.receive_json()["sessionId"]
        assert registry.get(sid) is not None  # parked for resume

        monkeypatch.setattr(Session, "start", slow_foreign_start)
        _as_household(monkeypatch, "intruder")
        with client.websocket_connect("/ws") as intruder:
            intruder.send_text(json.dumps({**start, "sessionId": sid}))
            time.sleep(0.1)  # let it take the lock and enter its slow start()
            _as_household(monkeypatch, "owner")
            with client.websocket_connect("/ws") as owner:
                began = time.monotonic()
                owner.send_text(json.dumps({**start, "sessionId": sid}))
                ready = owner.receive_json()
                waited = time.monotonic() - began
            assert ready == {"type": "session.ready", "sessionId": sid, "resumed": True}
            assert waited < 0.4, f"owner's resume waited {waited:.2f}s behind the intruder"
            assert intruder.receive_json()["sessionId"] != sid


def test_start_lock_serializes_one_id_and_forgets_it_after() -> None:
    async def run() -> None:
        order: list[str] = []

        async def hold(tag: str, sid: str | None) -> None:
            async with registry.start_lock(sid):
                order.append(f"{tag}+")
                await asyncio.sleep(0.01)
                order.append(f"{tag}-")

        await asyncio.gather(hold("a", "x"), hold("b", "x"), hold("n", None))
        # Same id: strictly one after the other. No id: never blocked.
        assert order.index("a-") < order.index("b+")
        assert order[0] == "a+" and order[1] == "n+"
        assert registry._start_locks == {}

    asyncio.run(run())


def test_resume_owner_check_store_error_is_an_error_frame_not_a_dropped_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store error in the cold-resume owner check used to escape the handler
    (ASGI 500, socket dropped without a close frame); it must answer like a failed
    start and keep the socket usable."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "session.start", "micSource": "g2-microphone"}))
            sid = ws.receive_json()["sessionId"]
            ws.send_text(json.dumps({"type": "session.end"}))

            def boom(*_a: object) -> None:
                raise RuntimeError("store down")

            monkeypatch.setattr(get_conversation_store(), "get", boom)
            ws.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "g2-microphone", "sessionId": sid}
                )
            )
            err = ws.receive_json()
            assert err["type"] == "error" and err["code"] == "internal"
            ws.send_text(json.dumps({"type": "ping", "t": 1}))
            assert ws.receive_json()["type"] == "pong"
            # The start lock was released on the way out: a retry still works.
            monkeypatch.undo()
            ws.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "g2-microphone", "sessionId": sid}
                )
            )
            assert ws.receive_json()["sessionId"] == sid


def test_resume_owner_check_fails_closed_on_a_transient_store_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner check is the cross-user gate (XERK-651): a store error there must
    refuse the start, not treat the recording as unowned and go ahead — even when
    the store recovers by the time start() reads it again."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "session.start", "micSource": "g2-microphone"}))
            sid = ws.receive_json()["sessionId"]
            ws.send_text(json.dumps({"type": "session.end"}))

            store = get_conversation_store()
            real_get = store.get
            calls = 0

            def flaky_get(*args: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("store blip")
                return real_get(*args)

            monkeypatch.setattr(store, "get", flaky_get)
            ws.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "g2-microphone", "sessionId": sid}
                )
            )
            assert ws.receive_json()["type"] == "error"
            assert calls == 1
            assert registry.count() == 0


def test_cold_resume_reopens_a_finished_conversation_as_live() -> None:
    """Regression (XERK-1502): a cold resume of a finalized conversation must read
    as live — not "ready" with the first sitting's ended_at — for the whole new
    sitting, and finalize again when that sitting ends."""

    def drain_to_pong(ws) -> None:
        while ws.receive_json()["type"] != "pong":
            pass

    store = get_conversation_store()
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "session.start", "micSource": "g2-microphone"}))
            sid = ws.receive_json()["sessionId"]
            ws.send_text(json.dumps({"type": "session.end"}))
            ws.send_text(json.dumps({"type": "ping", "t": 1}))
            drain_to_pong(ws)
        first = store.get("default", sid)
        assert first is not None and first.status == "ready" and first.ended_at is not None
        started_at, first_end = first.started_at, first.ended_at

        with client.websocket_connect("/ws") as ws2:
            ws2.send_text(
                json.dumps(
                    {"type": "session.start", "micSource": "g2-microphone", "sessionId": sid}
                )
            )
            assert ws2.receive_json()["sessionId"] == sid
            live = store.get("default", sid)
            assert live is not None
            assert live.status == "live"
            assert live.ended_at is None
            assert live.started_at == started_at  # the recording's start is kept
            ws2.send_text(json.dumps({"type": "session.end"}))
            ws2.send_text(json.dumps({"type": "ping", "t": 2}))
            drain_to_pong(ws2)

        done = store.get("default", sid)
        assert done is not None and done.status == "ready"
        assert done.ended_at is not None and done.ended_at >= first_end
