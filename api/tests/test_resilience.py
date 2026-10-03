"""Resilience — a failing STT seam degrades gracefully, never crashes.

A seam raising is caught, logged and *counted* instead of taking down the task or
the session — and the socket keeps working throughout.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from api.contract import ServerMessage
from api.main import app
from api.metrics import metrics
from api.persistence import get_audio_store, get_conversation_store
from api.session import Session


@pytest.fixture(autouse=True)
def _reset() -> None:
    get_conversation_store()._by_household.clear()
    get_audio_store()._blobs.clear()
    metrics.reset()
    yield
    metrics.reset()


def _noop_send(_: ServerMessage):
    async def send(_msg: ServerMessage) -> None:
        return None

    return send


def test_close_survives_a_failing_transcriber_flush() -> None:
    """A seam that raises from flush()/close() must not leak out of teardown — the
    conversation is still finalized and the failure is counted."""

    async def run() -> None:
        class BoomTranscriber:
            async def flush(self) -> None:
                raise RuntimeError("flush exploded")

            async def close(self) -> None:
                raise RuntimeError("close exploded")

        session = Session(_noop_send(None))
        session._transcriber = BoomTranscriber()  # type: ignore[assignment]
        session._conversations = None  # isolate: skip persistence in this unit
        await session.close()  # the raising flush/close must not leak out
        assert metrics.snapshot()["counters"]["stage.stt.errors"] >= 1

    asyncio.run(run())


def test_close_does_not_hang_when_flush_raises() -> None:
    """A flush that raises (STT timeout on the tail decode) must still close the
    transcriber: close() is what ends results(), and teardown awaits the pump that
    drains it — skipping it hung Session.close() forever."""
    from api.stt.stub import StubTranscriber

    class FlushTimesOut(StubTranscriber):
        async def flush(self) -> None:
            raise TimeoutError("stt stalled")

    async def run() -> None:
        session = Session(_noop_send(None))
        session._conversations = None  # isolate: skip persistence in this unit
        session._transcriber = FlushTimesOut()
        session._pump = asyncio.create_task(session._pump_results())
        await asyncio.wait_for(session.close(), timeout=5)
        assert session._pump.done()

    asyncio.run(run())


def test_pump_survives_a_failing_stt_seam() -> None:
    async def run() -> None:
        session = Session(_noop_send(None))
        session._transcriber = object()  # type: ignore[assignment]

        async def boom() -> None:
            raise RuntimeError("stt exploded")

        session._drain_results = boom  # type: ignore[assignment]
        # The pump must return cleanly (not raise) and count the failure.
        await session._pump_results()
        assert metrics.snapshot()["counters"]["stage.stt.errors"] == 1

    asyncio.run(run())


def test_health_reports_active_sessions_and_backends() -> None:
    client = TestClient(app)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["active_sessions"] == 0
    assert "stt_backend" in body


def test_metrics_endpoint_snapshots_counters() -> None:
    metrics.incr("caption.final", 3)
    metrics.observe("stage.stt.latency_ms", 12.5)
    client = TestClient(app)
    body = client.get("/metrics").json()
    assert body["counters"]["caption.final"] == 3
    assert body["latency_ms"]["stage.stt.latency_ms"]["count"] == 1
    assert body["active_sessions"] == 0


def test_ws_audio_error_is_isolated_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A throwing audio frame is counted and the socket stays open."""

    class FakeSession:
        def __init__(self, send, *, session_id=None, household=None, user_id=None) -> None:
            self._send = send
            self.session_id = session_id or "fake"
            self.household = household
            self.is_closed = False

        @property
        def current_send(self):
            return self._send

        def on_disconnect(self, fn) -> None:
            # The WS endpoint registers how to drop the socket, so an account
            # deletion can revoke a live capture rather than only finalize the
            # session behind it (XERK-236).
            self._closer = fn

        async def start(self, **_kwargs) -> None:
            from api.contract import SessionReady

            await self._send(
                SessionReady(type="session.ready", sessionId=self.session_id, resumed=False)
            )

        async def on_audio(self, _pcm: bytes) -> None:
            raise RuntimeError("bad frame")

        def detach(self, *, grace_seconds: float) -> None:
            self.is_closed = True

        async def close(self) -> None:
            return None

    monkeypatch.setattr("api.main.Session", FakeSession)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "session.start", "micSource": "phone-microphone"})
        ready = ws.receive_json()
        assert ready["type"] == "session.ready"
        ws.send_bytes(b"\x00\x01" * 160)  # triggers on_audio -> raises, must be isolated
        # The socket is still usable: a ping round-trips after the bad frame.
        ws.send_json({"type": "ping", "t": 7})
        pong = ws.receive_json()
        assert pong["type"] == "pong" and pong["t"] == 7

    assert metrics.snapshot()["counters"].get("audio.errors", 0) >= 1


def _persisting_session(transcriber) -> Session:
    session = Session(_noop_send(None))
    session._transcriber = transcriber
    session._pump = asyncio.create_task(session._pump_results())
    get_conversation_store().create(session._household, session.session_id)
    return session


def _is_live(session: Session) -> bool:
    conv = get_conversation_store().get(session._household, session.session_id)
    assert conv is not None
    return conv.status == "live"


def test_close_does_not_hang_when_transcriber_close_raises() -> None:
    """close() is what ends results(); if it raises, results() never ends, so the
    pump must be cancelled rather than awaited forever — and the conversation is
    still finalized (XERK-1460)."""
    from api.stt.stub import StubTranscriber

    class CloseRaises(StubTranscriber):
        async def close(self) -> None:
            raise RuntimeError("close exploded")

    async def run() -> None:
        session = _persisting_session(CloseRaises())
        await asyncio.wait_for(session.close(), timeout=5)
        assert session._pump.done()
        assert not _is_live(session)

    asyncio.run(run())


def test_close_persists_when_flush_raises_cancelled_error() -> None:
    """A CancelledError out of the flush seam (a BaseException) must not skip
    closing the transcriber or persisting the conversation (XERK-1460)."""
    from api.stt.stub import StubTranscriber

    class FlushCancelled(StubTranscriber):
        async def flush(self) -> None:
            raise asyncio.CancelledError

    async def run() -> None:
        session = _persisting_session(FlushCancelled())
        await asyncio.wait_for(session.close(), timeout=5)
        assert session._pump.done()
        assert not _is_live(session)

    asyncio.run(run())


def test_cancelling_close_still_persists() -> None:
    """Cancelling the task running close() (e.g. server shutdown) must not lose the
    conversation: teardown is shielded and finishes on its own (XERK-1460)."""
    from api.stt.stub import StubTranscriber

    gate = asyncio.Event()

    class SlowFlush(StubTranscriber):
        async def flush(self) -> None:
            await gate.wait()

    async def run() -> None:
        session = _persisting_session(SlowFlush())
        closer = asyncio.create_task(session.close())
        await asyncio.sleep(0.05)
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        gate.set()
        assert session._teardown is not None
        await asyncio.wait_for(session._teardown, timeout=5)
        assert not _is_live(session)

    asyncio.run(run())
