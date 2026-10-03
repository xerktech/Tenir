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
from api.config import settings
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

        def drop_disconnect(self, fn) -> None:
            self._closer = None

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


def test_close_persists_when_transcriber_close_raises_cancelled_error() -> None:
    """A CancelledError out of transcriber.close() is a seam failure like any other:
    the pump is cancelled and the conversation still finalized (XERK-1460)."""
    from api.stt.stub import StubTranscriber

    class CloseCancelled(StubTranscriber):
        async def close(self) -> None:
            raise asyncio.CancelledError

    async def run() -> None:
        session = _persisting_session(CloseCancelled())
        await asyncio.wait_for(session.close(), timeout=5)
        assert session._pump.done()
        assert not _is_live(session)

    asyncio.run(run())


def test_second_close_waits_for_an_in_flight_teardown() -> None:
    """A close() racing one whose caller was cancelled returns only once the
    conversation is finalized, not while it is still live (XERK-1460)."""
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
        second = asyncio.create_task(session.close())
        await asyncio.sleep(0.05)
        assert not second.done()
        gate.set()
        await asyncio.wait_for(second, timeout=5)
        assert not _is_live(session)

    asyncio.run(run())


def test_shutdown_waits_for_orphaned_teardowns() -> None:
    """Lifespan shutdown drains teardowns whose close() caller was cancelled, so the
    process can't exit before they persist (XERK-1460)."""
    from api.main import close_all_sessions
    from api.session import _teardowns
    from api.stt.stub import StubTranscriber

    class SlowFlush(StubTranscriber):
        async def flush(self) -> None:
            await asyncio.sleep(0.2)

    async def run() -> None:
        session = _persisting_session(SlowFlush())
        closer = asyncio.create_task(session.close())
        await asyncio.sleep(0.05)
        closer.cancel()
        await asyncio.wait_for(close_all_sessions(), timeout=5)
        assert not _is_live(session)
        assert not _teardowns

    asyncio.run(run())


def test_cancelling_the_teardown_itself_is_not_swallowed() -> None:
    """Event-loop shutdown cancels the teardown task directly; that cancellation
    must propagate, not be swallowed into an unbounded wait on a wedged seam."""
    from api.stt.stub import StubTranscriber

    class Wedged(StubTranscriber):
        async def flush(self) -> None:
            await asyncio.Event().wait()

        async def close(self) -> None:
            await asyncio.Event().wait()

    async def run() -> None:
        session = _persisting_session(Wedged())
        closer = asyncio.create_task(session.close())
        await asyncio.sleep(0.05)
        closer.cancel()
        assert session._teardown is not None
        session._teardown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(session._teardown, timeout=5)
        session._pump.cancel()

    asyncio.run(run())


def test_lifespan_shutdown_drains_orphaned_teardowns() -> None:
    """The session is unregistered before close(), so shutdown never sees it in the
    registry: lifespan must drain in-flight teardowns or the process exits before
    they persist (XERK-1460)."""
    from api.main import app, lifespan
    from api.stt.stub import StubTranscriber

    class SlowFlush(StubTranscriber):
        async def flush(self) -> None:
            await asyncio.sleep(0.2)

    async def run() -> Session:
        async with lifespan(app):
            session = _persisting_session(SlowFlush())
            closer = asyncio.create_task(session.close())
            await asyncio.sleep(0.05)
            closer.cancel()
            assert _is_live(session)
        return session

    session = asyncio.run(run())
    assert not _is_live(session)


def test_close_is_bounded_when_transcriber_close_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged transcriber.close() is bounded like flush(): teardown times it out,
    cancels the pump and still persists (XERK-1460)."""
    import api.session as session_mod
    from api.stt.stub import StubTranscriber

    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 0.1)

    class CloseHangs(StubTranscriber):
        async def close(self) -> None:
            await asyncio.Event().wait()

    async def run() -> None:
        session = _persisting_session(CloseHangs())
        await asyncio.wait_for(session.close(), timeout=5)
        assert session._pump.done()
        assert not _is_live(session)

    asyncio.run(run())


def test_start_that_raises_late_does_not_leak_the_half_started_session() -> None:
    """XERK-1511: start() creates the pump/warmup tasks and the live conversation row
    before its final session.ready send. A send that raises there (the socket just
    died) used to leave those tasks running and the row 'live' with nobody to close
    them; start() must tear its partial state down before re-raising."""

    async def run() -> None:
        async def dying_send(msg: ServerMessage) -> None:
            if msg.type == "session.ready":
                raise RuntimeError("socket gone")

        session = Session(dying_send, household="h1")
        with pytest.raises(RuntimeError, match="socket gone"):
            await session.start(mic_source="phone", source_lang=None)
        assert session.is_closed
        assert session._pump is not None and session._pump.done()
        conv = get_conversation_store().get("h1", session.session_id)
        assert conv is not None and conv.status == "ready"

    asyncio.run(run())


def test_start_that_raises_in_create_does_not_leak_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store error in conversations.create() came after the translation worker and
    music scan were spawned; those must not outlive the failed start."""

    monkeypatch.setattr(settings, "translation_backend", "stub")
    monkeypatch.setattr(settings, "music_backend", "stub")

    async def run() -> None:
        session = Session(_noop_send(None), household="h1")

        def boom(*_a, **_k) -> None:
            raise RuntimeError("store down")

        monkeypatch.setattr(session._conversations, "create", boom)
        before = asyncio.all_tasks()
        with pytest.raises(RuntimeError, match="store down"):
            await session.start(mic_source="phone", source_lang=None)
        assert session.is_closed
        leaked = [t for t in asyncio.all_tasks() - before if not t.done()]
        assert session._translation_worker is None and session._music_scan is None
        assert leaked == []

    asyncio.run(run())


def test_start_cancelled_during_create_does_not_leave_the_row_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XERK-1529: a cancel landing while conversations.create() runs in its thread
    can't stop that thread. The failed-start cleanup ran finish() first, matched no
    row, and the INSERT then committed 'live' after teardown had already finished."""
    import threading

    async def run() -> None:
        session = Session(_noop_send(None), household="h1")
        store = session._conversations
        real_create = store.create
        entered, release = threading.Event(), threading.Event()

        def slow_create(*a, **k):
            entered.set()
            release.wait(5)
            return real_create(*a, **k)

        monkeypatch.setattr(store, "create", slow_create)
        start = asyncio.create_task(session.start(mic_source="phone", source_lang=None))
        await asyncio.to_thread(entered.wait, 5)
        start.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await start
        assert session.is_closed
        await asyncio.sleep(0.2)  # let a still-running INSERT land
        conv = store.get("h1", session.session_id)
        assert conv is not None and conv.status == "ready" and conv.ended_at is not None

    asyncio.run(run())


def test_deadline_cancel_of_a_teardown_waiting_on_create_still_finishes_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XERK-1529 QA: the shutdown deadline cancelling a failed start's teardown while
    it waits on the create thread skipped finish(), leaving the row 'live'."""
    import threading

    async def run() -> None:
        session = Session(_noop_send(None), household="h1")
        store = session._conversations
        real_create = store.create
        entered, release = threading.Event(), threading.Event()

        def slow_create(*a, **k):
            entered.set()
            release.wait(5)
            return real_create(*a, **k)

        monkeypatch.setattr(store, "create", slow_create)
        start = asyncio.create_task(session.start(mic_source="phone", source_lang=None))
        await asyncio.to_thread(entered.wait, 5)
        start.cancel()
        while session._teardown is None:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # teardown is now parked on the create
        session._teardown.cancel()
        release.set()
        await asyncio.wait({session._teardown, start})
        assert session._teardown.cancelled()
        conv = store.get("h1", session.session_id)
        assert conv is not None and conv.status == "ready" and conv.ended_at is not None

    asyncio.run(run())


def test_failed_resume_does_not_evict_the_sitting_still_tearing_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XERK-1511 QA: sitting B resumed behind A (still draining a slow flush) and
    failed at session.ready. B's own cleanup took A's place in the closing registry
    and popped it, so the next resume C found no prior, read the stores before A's
    tail final was written and restarted its timeline at 0 (audio backend off)."""
    from api.session import _closing
    from api.stt.stub import StubTranscriber

    monkeypatch.setattr("api.session.get_audio_store", lambda: None)

    async def dying_send(msg: ServerMessage) -> None:
        if msg.type == "session.ready":
            raise RuntimeError("socket gone")

    async def run() -> None:
        a = Session(_noop_send(None), household="h", session_id="conv1")
        await a.start(mic_source="phone", source_lang=None)
        await a.on_audio(b"\x00\x00" * 24000)  # 1.5 s, finalized only by the flush
        a_end = a._current_audio_ms()
        orig = StubTranscriber.flush

        async def slow_flush(self) -> None:
            await asyncio.sleep(0.5)
            await orig(self)

        monkeypatch.setattr(StubTranscriber, "flush", slow_flush)
        a_close = asyncio.create_task(a.close())
        await asyncio.sleep(0.05)
        monkeypatch.setattr(StubTranscriber, "flush", orig)

        b = Session(dying_send, household="h", session_id="conv1")
        with pytest.raises(RuntimeError, match="socket gone"):
            await b.start(mic_source="phone", source_lang=None)
        assert not a._teardown.done()
        assert _closing.get(("h", "conv1")) is a

        c = Session(_noop_send(None), household="h", session_id="conv1")
        await c.start(mic_source="phone", source_lang=None)
        assert c._start_offset_ms == a_end
        await a_close
        await c.close()

    asyncio.run(run())


def test_start_that_fails_before_the_row_leaves_a_finished_recording_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cold resume that never reached conversations.create must not run
    finish() on the existing recording: that rewrote its ended_at."""

    async def run() -> None:
        first = Session(_noop_send(None), household="h", session_id="conv1")
        await first.start(mic_source="phone", source_lang=None)
        await first.close()
        ended = get_conversation_store().get("h", "conv1").ended_at
        assert ended is not None

        def boom(**_k):
            raise RuntimeError("no model")

        monkeypatch.setattr("api.session.make_transcriber", boom)
        again = Session(_noop_send(None), household="h", session_id="conv1")
        with pytest.raises(RuntimeError, match="no model"):
            await again.start(mic_source="phone", source_lang=None)
        assert again.is_closed
        conv = get_conversation_store().get("h", "conv1")
        assert conv.ended_at == ended and conv.status == "ready"

    asyncio.run(run())


def test_cancelled_start_still_tears_down() -> None:
    """A cancel (the WS handler cancelled mid-start) is cleaned up like an error."""

    async def run() -> None:
        reached = asyncio.Event()

        async def hanging_send(msg: ServerMessage) -> None:
            if msg.type == "session.ready":
                reached.set()
                await asyncio.Event().wait()

        session = Session(hanging_send, household="h1")
        before = asyncio.all_tasks()
        task = asyncio.create_task(session.start(mic_source="phone", source_lang=None))
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.is_closed
        assert [t for t in asyncio.all_tasks() - before if not t.done()] == []
        assert get_conversation_store().get("h1", session.session_id).status == "ready"

    asyncio.run(run())


def test_failed_start_whose_cleanup_raises_reraises_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close() that raises during that cleanup is logged; the caller still sees
    the start failure, not the cleanup's."""

    async def run() -> None:
        async def dying_send(msg: ServerMessage) -> None:
            if msg.type == "session.ready":
                raise RuntimeError("socket gone")

        session = Session(dying_send, household="h1")

        async def bad_close() -> None:
            raise ValueError("cleanup broke")

        monkeypatch.setattr(session, "close", bad_close)
        with pytest.raises(RuntimeError, match="socket gone"):
            await session.start(mic_source="phone", source_lang=None)

    asyncio.run(run())


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
