"""Reconnect-with-resume.

A dropped socket keeps its session (and transcriber state) alive for a grace
window so a reconnect carrying the same id rebinds to it instead of starting a
fresh one.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import registry
from api.contract import Pong, ServerMessage
from api.main import app
from api.persistence import get_audio_store, get_conversation_store
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
