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

    def flaky(*args) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db blip")
        real(*args)

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
