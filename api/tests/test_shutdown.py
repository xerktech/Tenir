"""Pod shutdown must finalize every live session inside the grace period (XERK-1458).

The lifespan used to close sessions one at a time. Against a hung model each close
can take ~30 s (STT flush + translation drain), which is the whole prod grace
period, so every later session was SIGKILLed before it persisted its audio and
its conversation was left "live".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from api import registry
from api import session as session_mod
from api.contract import CaptionFinal, CaptionPartial
from api.main import close_all_sessions
from api.persistence import audio_key, get_audio_store, get_conversation_store
from api.persistence.wav import wav_to_pcm16
from api.session import Session

CHUNK = b"\x11\x22" * 1600


class HangingFlush:
    """An STT stream whose tail decode never returns (a hung upstream)."""

    def __init__(self) -> None:
        self.closed = False

    async def warmup(self) -> None:
        pass

    async def push(self, pcm: bytes) -> None:
        pass

    async def results(self) -> AsyncIterator[CaptionPartial | CaptionFinal]:
        while not self.closed:
            await asyncio.sleep(0.01)
        if False:  # pragma: no cover - makes this an async generator
            yield  # type: ignore[unreachable]

    async def flush(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    get_conversation_store()._by_household.clear()
    get_audio_store()._blobs.clear()
    monkeypatch.setattr(
        session_mod, "make_transcriber", lambda source_lang=None, **kw: HangingFlush()
    )
    yield
    for s in registry.active():
        registry.unregister(s)


async def _live_sessions(n: int) -> list[Session]:
    async def send(_msg) -> None:
        pass

    sessions = []
    for _ in range(n):
        s = Session(send, household="hh")
        await s.start(mic_source="phone-microphone", source_lang=None)
        await s.on_audio(CHUNK)
        registry.register(s)
        sessions.append(s)
    return sessions


def _assert_persisted(sessions: list[Session]) -> None:
    for s in sessions:
        conv = get_conversation_store().get("hh", s.session_id)
        assert conv is not None and conv.status == "ready", s.session_id
        assert get_audio_store().get(audio_key("hh", s.session_id)), "audio was lost"


def test_sessions_close_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 0.5)

    async def run() -> None:
        sessions = await _live_sessions(3)
        t0 = time.monotonic()
        await close_all_sessions(deadline=5)
        # Sequential closes would take 3 x 0.5 s.
        assert time.monotonic() - t0 < 1.2
        assert registry.active() == []
        _assert_persisted(sessions)
        # Finished closes leave the in-flight set, or it pins every Session forever.
        assert not session_mod._teardowns

    asyncio.run(run())


def test_closes_past_the_deadline_still_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    """A close the deadline cancels has already retained its audio and still
    finalizes its conversation."""
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 30)

    async def run() -> None:
        sessions = await _live_sessions(3)
        t0 = time.monotonic()
        await close_all_sessions(deadline=0.2)
        assert time.monotonic() - t0 < 2
        _assert_persisted(sessions)

    asyncio.run(run())


def test_audio_arriving_during_close_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 0.3)

    async def run() -> None:
        (s,) = await _live_sessions(1)
        closing = asyncio.create_task(s.close())
        await asyncio.sleep(0.1)  # close() has retained the first chunk, now flushing
        await s.on_audio(CHUNK)
        await closing
        pcm = wav_to_pcm16(get_audio_store().get(audio_key("hh", s.session_id)))
        assert pcm == CHUNK * 2

    asyncio.run(run())


def test_one_failing_close_does_not_skip_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 0.05)

    async def run() -> None:
        bad, *good = await _live_sessions(3)

        async def boom() -> None:
            raise RuntimeError("store down")

        monkeypatch.setattr(bad, "_persist", boom)
        await close_all_sessions(deadline=5)
        _assert_persisted(good)

    asyncio.run(run())


def test_no_sessions_is_a_no_op() -> None:
    asyncio.run(close_all_sessions(deadline=0))


def _stored_pcm(s: Session) -> bytes:
    return wav_to_pcm16(get_audio_store().get(audio_key("hh", s.session_id)))


def test_audio_is_stored_before_the_model_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    """The early retain is the SIGKILL protection: the WAV must be on the store
    while the STT flush is still hung, not only once close() returns."""
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 30)

    async def run() -> None:
        (s,) = await _live_sessions(1)
        closing = asyncio.create_task(s.close())
        await asyncio.sleep(0.2)
        assert not closing.done()
        assert _stored_pcm(s) == CHUNK
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)

    asyncio.run(run())


def test_audio_arriving_during_the_store_write_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """The buffer is trimmed by what was written, not cleared, so audio landing
    while the early retain's write is in flight is stored by the final persist."""
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 0.05)
    store = get_audio_store()
    real_put = store.put

    async def run() -> None:
        (s,) = await _live_sessions(1)
        late = asyncio.get_running_loop().create_future()

        def put(key: str, data: bytes) -> None:
            if not late.done():
                late.get_loop().call_soon_threadsafe(late.set_result, None)
                time.sleep(0.2)
            real_put(key, data)

        monkeypatch.setattr(store, "put", put)
        closing = asyncio.create_task(s.close())
        await late
        await s.on_audio(CHUNK)  # arrives while the first put is still writing
        await closing
        assert _stored_pcm(s) == CHUNK * 2

    asyncio.run(run())


def test_a_failed_set_audio_key_does_not_duplicate_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """QA: put succeeded but set_audio_key failed once, the buffer stayed untrimmed,
    and the final persist stored it behind its own stored copy (A+A)."""
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 0.05)
    conversations = get_conversation_store()
    real = conversations.set_audio_key
    calls = 0

    def flaky(*args) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db blip")
        return real(*args)

    monkeypatch.setattr(conversations, "set_audio_key", flaky)

    async def run() -> None:
        (s,) = await _live_sessions(1)
        await s.close()
        assert _stored_pcm(s) == CHUNK
        conv = conversations.get("hh", s.session_id)
        assert conv.audio_key == audio_key("hh", s.session_id), "key must be retried"
        assert conv.status == "ready"

    asyncio.run(run())


def test_cancel_during_the_first_store_write_still_finalizes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA: a deadline cancel landing in the early retain skipped finish(). It must
    finalize, and must not store the still-untrimmed buffer a second time."""
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 30)
    store = get_audio_store()
    real_put = store.put

    puts: list[str] = []

    def slow_put(key: str, data: bytes) -> None:
        puts.append(key)
        time.sleep(0.5)
        real_put(key, data)

    monkeypatch.setattr(store, "put", slow_put)

    async def run() -> None:
        sessions = await _live_sessions(2)
        await close_all_sessions(deadline=0.1)
        _assert_persisted(sessions)
        for s in sessions:
            assert _stored_pcm(s) == CHUNK
        # The cancelled write was awaited, not abandoned and redone: a second
        # write racing the orphaned one is how the audio got stored twice.
        assert sorted(puts) == sorted(audio_key("hh", s.session_id) for s in sessions)

    asyncio.run(run())


def test_shutdown_waits_for_a_close_already_under_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """QA: a lapsed grace window unregisters its session before closing it, so a
    shutdown that only walked the registry returned at once and the process died
    with that close mid-flight, leaving the conversation live."""
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 0.3)

    async def run() -> None:
        (s,) = await _live_sessions(1)
        s.detach(grace_seconds=0.01)
        await asyncio.sleep(0.1)  # grace lapsed: unregistered, close() is flushing
        assert registry.active() == []
        assert get_conversation_store().get("hh", s.session_id).status == "live"
        await close_all_sessions(deadline=5)
        _assert_persisted([s])

    asyncio.run(run())


def test_a_close_hung_after_cancel_does_not_hold_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even the cancelled closes' finalize is bounded, so a hung store can't keep
    the process alive into the SIGKILL."""
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 30)
    monkeypatch.setattr("api.main._SHUTDOWN_FINALIZE_S", 0.1)

    async def run() -> None:
        (s,) = await _live_sessions(1)

        async def hung() -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(s, "_persist", hung)
        t0 = time.monotonic()
        # Own timeout so a missing bound fails here instead of hanging the suite.
        await asyncio.wait_for(close_all_sessions(deadline=0.1), timeout=2)
        assert time.monotonic() - t0 < 1
        for task in session_mod.teardowns_in_flight():
            task.cancel()

    asyncio.run(run())


def test_a_deadline_cancel_at_the_music_scan_join_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA: the scan join swallowed every CancelledError, so a deadline cancel landing
    there ran on into music.close() (unbounded) and never reached _persist()."""
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 0.01)

    class HungMusic:
        async def close(self) -> None:
            await asyncio.Event().wait()

    async def slow_to_stop() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(1)  # still winding down when the deadline lands
            raise

    async def run() -> None:
        (s,) = await _live_sessions(1)
        s._music_scan = asyncio.create_task(slow_to_stop())
        s._music = HungMusic()
        await asyncio.wait_for(close_all_sessions(deadline=0.5), timeout=3)
        _assert_persisted([s])

    asyncio.run(run())


async def _ws_handler_hung_in_start(monkeypatch: pytest.MonkeyPatch) -> asyncio.Task[None]:
    """Run the /ws handler as uvicorn would, parked in session.start's account check
    (a hung database) so it is still running when the graceful-shutdown deadline hits."""
    from api import main

    in_check = asyncio.Event()

    async def hung_check(_user_id: str) -> bool:
        in_check.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(main, "_account_exists", hung_check)
    inbound: asyncio.Queue[dict] = asyncio.Queue()
    for event in (
        {"type": "websocket.connect"},
        {
            "type": "websocket.receive",
            "text": '{"type": "session.start", "micSource": "phone-microphone"}',
        },
    ):
        inbound.put_nowait(event)

    async def send(_event: dict) -> None:
        pass

    scope = {
        "type": "websocket",
        "path": "/ws",
        "raw_path": b"/ws",
        "root_path": "",
        "scheme": "ws",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
        "subprotocols": [],
        "asgi": {"version": "3.0"},
        "state": {},
    }
    task = asyncio.create_task(main.app(scope, inbound.get, send))
    await asyncio.wait_for(in_check.wait(), timeout=2)
    return task


def test_graceful_shutdown_cancel_ends_the_ws_handler_quietly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """XERK-1530: uvicorn logs whatever a cancelled handler raises as "Exception in ASGI
    application" with a traceback, so its shutdown cancel must not escape the handler."""
    from api.main import _UVICORN_SHUTDOWN_CANCEL

    async def run() -> None:
        task = await _ws_handler_hung_in_start(monkeypatch)
        task.cancel(msg=_UVICORN_SHUTDOWN_CANCEL)
        await asyncio.wait_for(task, timeout=2)  # returns: nothing for uvicorn to log

    with caplog.at_level("INFO", logger="api"):
        asyncio.run(run())
    assert "cancelled at the graceful-shutdown deadline" in caplog.text


def test_any_other_cancel_still_propagates_out_of_the_ws_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        task = await _ws_handler_hung_in_start(monkeypatch)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())


def test_the_shutdown_cancel_message_is_uvicorns() -> None:
    """The handler recognises the shutdown cancel by uvicorn's message alone. If uvicorn
    rewords it (it is not pinned upward), the tracebacks come back with CI green."""
    import inspect

    import uvicorn.server

    from api.main import _UVICORN_SHUTDOWN_CANCEL

    assert f'msg="{_UVICORN_SHUTDOWN_CANCEL}"' in inspect.getsource(uvicorn.server)


def _http_scope(path: str = "/slow") -> dict[str, object]:
    return {"type": "http", "method": "GET", "path": path, "headers": []}


async def _never() -> dict[str, object]:
    await asyncio.Event().wait()
    return {}


def _run_cancelled_http(
    respond_first: bool, msg: str | None, path: str = "/slow"
) -> tuple[list[dict[str, object]], asyncio.Task[None]]:
    """Run an http request through ShutdownCancelMiddleware, hang it, cancel it with msg."""
    from api.main import ShutdownCancelMiddleware

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def slow_route(scope: object, receive: object, send: object) -> None:
        if respond_first:
            await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await asyncio.Event().wait()

    async def run() -> asyncio.Task[None]:
        task = asyncio.create_task(
            ShutdownCancelMiddleware(slow_route)(_http_scope(path), _never, send)
        )
        await asyncio.sleep(0)
        task.cancel(msg=msg)
        await asyncio.wait([task], timeout=2)
        return task

    return sent, asyncio.run(run())


def test_graceful_shutdown_cancel_answers_a_hung_http_request_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """XERK-1602: uvicorn logs the shutdown cancel of a hung HTTP request as "Exception in
    ASGI application" with a traceback, then sends a 500. The middleware swallows that one
    cancel and answers a retryable 503 instead."""
    from api.main import _UVICORN_SHUTDOWN_CANCEL

    with caplog.at_level("INFO", logger="api"):
        sent, task = _run_cancelled_http(respond_first=False, msg=_UVICORN_SHUTDOWN_CANCEL)
    assert task.done() and not task.cancelled() and task.exception() is None
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 503
    assert "http '/slow' cancelled at the graceful-shutdown deadline" in caplog.text


def test_graceful_shutdown_cancel_after_the_response_started_sends_nothing_more() -> None:
    # A second http.response.start would be a protocol error; uvicorn closes the connection.
    from api.main import _UVICORN_SHUTDOWN_CANCEL

    sent, task = _run_cancelled_http(respond_first=True, msg=_UVICORN_SHUTDOWN_CANCEL)
    assert task.done() and not task.cancelled() and task.exception() is None
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_any_other_cancel_still_propagates_out_of_an_http_request() -> None:
    sent, task = _run_cancelled_http(respond_first=False, msg=None)
    assert task.cancelled()
    assert sent == []


def test_shutdown_cancel_middleware_wraps_every_other_middleware() -> None:
    """Inside BaseHTTPMiddleware it would miss what that re-raises; starlette builds the
    stack from user_middleware in order, so index 0 is the outermost."""
    from api import main

    assert main.app.user_middleware[0].cls is main.ShutdownCancelMiddleware


def test_shutdown_cancel_middleware_passes_lifespan_through() -> None:
    from api.main import ShutdownCancelMiddleware

    seen: list[str] = []

    async def inner(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(str(scope["type"]))

    asyncio.run(ShutdownCancelMiddleware(inner)({"type": "lifespan"}, _never, _never))  # type: ignore[arg-type]
    assert seen == ["lifespan"]


def test_shutdown_cancel_log_line_cannot_be_forged_through_the_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # scope["path"] is percent-decoded: a %0a in the URL is a real newline by now.
    from api.main import _UVICORN_SHUTDOWN_CANCEL

    with caplog.at_level("INFO", logger="api"):
        _run_cancelled_http(False, _UVICORN_SHUTDOWN_CANCEL, path="/a\nINFO api FORGED")
    assert "\n" not in caplog.records[-1].getMessage()
