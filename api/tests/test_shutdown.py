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
