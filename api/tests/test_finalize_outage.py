"""A session.end during a database outage still finalizes the recording (XERK-1531)."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from api.persistence import get_audio_store, get_conversation_store
from api.persistence.audio import InMemoryAudioStore
from api.persistence.conversations import InMemoryConversationStore
from api.persistence.postgres import DatabaseUnavailable
from api.session import Session


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    convs = get_conversation_store()
    audio = get_audio_store()
    assert isinstance(convs, InMemoryConversationStore)
    assert isinstance(audio, InMemoryAudioStore)
    convs._by_household.clear()
    audio._blobs.clear()


def _voice_chunk(*, ms: int = 100, freq: int = 200) -> bytes:
    n = 16000 * ms // 1000
    t = np.arange(n) / 16000.0
    return (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16).tobytes()


def _outage(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Make every conversation write raise like a Postgres outage while ``down``."""
    state = {"down": False}
    store = get_conversation_store()
    for name in ("add_segment", "finish", "set_audio_key"):
        real = getattr(store, name)

        def call(*args, _real=real, **kwargs):
            if state["down"]:
                raise DatabaseUnavailable("database unavailable")
            return _real(*args, **kwargs)

        monkeypatch.setattr(store, name, call)
    return state


def test_end_during_outage_finalizes_once_the_database_is_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.session as session_mod

    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01, raising=False)
    db = _outage(monkeypatch)

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, session_id="conv-outage")
        await session.start(mic_source="phone-microphone", source_lang=None)
        for _ in range(30):  # ~3s -> stub finals [0,2000] + flush tail [2000,3000]
            await session.on_audio(_voice_chunk())
        db["down"] = True
        await session.close()  # session.end mid-outage: must not raise
        conv = get_conversation_store().get("default", "conv-outage")
        assert conv is not None and conv.status == "live"

        db["down"] = False
        for _ in range(200):
            conv = get_conversation_store().get("default", "conv-outage")
            if conv is not None and conv.status == "ready":
                break
            await asyncio.sleep(0.01)
        assert conv is not None
        assert conv.status == "ready" and conv.ended_at is not None
        # Every turn recorded before the end survived the outage, tail included.
        assert [s.start_ms for s in conv.segments] == [0, 2000]
        assert conv.audio_key is not None

    asyncio.run(run())


def test_outage_mid_session_keeps_captions_and_stores_held_turns_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed segment write used to kill the result pump: captions stopped for the
    rest of the session and every later turn was lost. Now the turn is held and
    stored with the next one once the database is back."""
    db = _outage(monkeypatch)

    async def run() -> None:
        captions: list[str] = []

        async def send(msg) -> None:
            if msg.type == "caption.final":
                captions.append(msg.segmentId)

        session = Session(send, session_id="conv-mid")
        await session.start(mic_source="phone-microphone", source_lang=None)
        db["down"] = True
        for _ in range(20):  # ~2s -> the stub's first final [0,2000]
            await session.on_audio(_voice_chunk())
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(captions) == 1
        conv = get_conversation_store().get("default", "conv-mid")
        assert conv is not None and conv.segments == []

        db["down"] = False
        for _ in range(10):  # ~1s more; close() flushes the tail [2000,3000]
            await session.on_audio(_voice_chunk())
        await session.close()

        assert len(captions) == 2, "the pump died on the outage"
        conv = get_conversation_store().get("default", "conv-mid")
        assert conv is not None and conv.status == "ready"
        assert [s.start_ms for s in conv.segments] == [0, 2000]

    asyncio.run(run())


def test_finalize_retry_gives_up_on_a_non_outage_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.session as session_mod

    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
    db = _outage(monkeypatch)

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, session_id="conv-fault")
        await session.start(mic_source="phone-microphone", source_lang=None)
        db["down"] = True
        await session.close()
        retries = list(session_mod._finalize_retries)
        assert len(retries) == 1

        def broken(*_a, **_k):
            raise ValueError("not an outage")

        monkeypatch.setattr(get_conversation_store(), "finish", broken)
        db["down"] = False
        await asyncio.wait_for(retries[0], timeout=5)
        assert not session_mod._finalize_retries

    asyncio.run(run())
