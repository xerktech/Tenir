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
        def __init__(self, send, *, session_id=None, household=None) -> None:
            self._send = send
            self.session_id = session_id or "fake"
            self.household = household
            self.is_closed = False

        @property
        def current_send(self):
            return self._send

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
