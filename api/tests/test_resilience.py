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


def test_finals_backlogged_by_an_stt_outage_are_stored_not_pushed_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After an STT outage the queued finals land as one burst (XERK-1447). A final
    whose audio arrived longer ago than _STALE_FINAL_S goes to the stored transcript
    only — not to the caption band, translation or cues — while a fresh one is pushed."""
    import api.session as session_mod
    from api.config import settings
    from api.contract import CaptionFinal, MicSource
    from api.stt.stub import StubTranscriber

    monkeypatch.setattr(settings, "translation_backend", "stub")

    class Outage(StubTranscriber):
        """Holds every final until release(), as a hung upstream holds the queue."""

        async def push(self, pcm: bytes) -> None:
            self._total_bytes += len(pcm)

        async def release(self, *ends_ms: int) -> None:
            start = 0
            for end in ends_ms:
                await self._queue.put(
                    CaptionFinal(
                        type="caption.final",
                        segmentId=f"seg-{end}",
                        text=f"turn {end}",
                        lang="en",
                        startMs=start,
                        endMs=end,
                    )
                )
                start = end

    outage = Outage()
    monkeypatch.setattr(session_mod, "make_transcriber", lambda *a, **k: outage)

    async def run() -> None:
        sent: list[ServerMessage] = []

        async def sender(m: ServerMessage) -> None:
            sent.append(m)

        session = Session(sender, household="default")
        await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)
        await session.on_audio(b"\x00" * 64000)  # 0-2000 ms, spoken before the outage
        # That audio arrived 40 s ago; the next turn's audio arrives now.
        session._audio_arrivals = type(session._audio_arrivals)(
            ((ms, t - 40.0) for ms, t in session._audio_arrivals),
            maxlen=session._audio_arrivals.maxlen,
        )
        await session.on_audio(b"\x00" * 64000)  # 2000-4000 ms
        considered: list[str] = []
        session._consider_translation = lambda f: considered.append(f"tr:{f.segmentId}")  # type: ignore[method-assign]
        session._consider_cue = lambda f: considered.append(f"cue:{f.segmentId}")  # type: ignore[method-assign]
        await outage.release(2000, 4000)
        await session.close()

        live = [m.segmentId for m in sent if isinstance(m, CaptionFinal)]
        assert live == ["seg-4000"]
        conv = get_conversation_store().get("default", session.session_id)
        assert conv is not None
        assert [s.segment_id for s in conv.segments] == ["seg-2000", "seg-4000"]
        # Only the fresh turn reached translation and cue consideration.
        assert considered == ["tr:seg-4000", "cue:seg-4000"]
        assert metrics.snapshot()["counters"]["caption.final_stale"] == 1

    asyncio.run(run())


def test_final_with_no_dated_audio_is_not_stale() -> None:
    """A final past the last audio sample (or before any audio) can't be dated; it is
    treated as fresh rather than silently withheld from the caption band."""
    from api.contract import CaptionFinal

    session = Session(_noop_send(None))
    final = CaptionFinal(type="caption.final", segmentId="s", text="t", startMs=0, endMs=500)
    assert session._final_age_s(final) == 0.0  # before any audio
    session._audio_arrivals.append((400, 0.0))  # one push, dated long ago
    assert session._final_age_s(final) == 0.0  # final runs past the last sample
