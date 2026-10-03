"""A session.end during a database outage still finalizes the recording (XERK-1531)."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from api.persistence import get_audio_store, get_conversation_store
from api.persistence.audio import InMemoryAudioStore
from api.persistence.conversations import InMemoryConversationStore
from api.persistence.postgres import DatabaseUnavailable
import api.session as session_mod
from api.config import settings
from api.contract import CaptionFinal
from api.persistence import Segment
from api.session import Session


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    session_mod._unfinalized.clear()
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


_WRITES = ("add_segment", "set_segment_translation", "finish", "set_audio_key")


def _outage(monkeypatch: pytest.MonkeyPatch, *names: str) -> dict[str, bool]:
    """Make conversation writes (default: all of them) raise like a Postgres outage
    while ``down``."""
    state = {"down": False}
    store = get_conversation_store()
    for name in names or _WRITES:
        real = getattr(store, name)

        def call(*args, _real=real, **kwargs):
            if state["down"]:
                raise DatabaseUnavailable("database unavailable")
            return _real(*args, **kwargs)

        monkeypatch.setattr(store, name, call)
    return state


async def _until_ready(conversation_id: str):
    for _ in range(200):
        conv = get_conversation_store().get("default", conversation_id)
        if conv is not None and conv.status == "ready":
            return conv
        await asyncio.sleep(0.01)
    raise AssertionError(f"{conversation_id} never finalized")


def test_end_during_outage_finalizes_once_the_database_is_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
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

        await asyncio.sleep(0.1)  # several retries while still down
        conv = get_conversation_store().get("default", "conv-outage")
        assert conv is not None and conv.status == "live"

        db["down"] = False
        conv = await _until_ready("conv-outage")
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


def test_finalize_waits_for_an_audio_key_the_outage_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only set_audio_key fails: finish() must not mark the row ready without the key
    to its stored WAV, or History can't play it."""
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
    db = _outage(monkeypatch, "set_audio_key")

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, session_id="conv-key")
        await session.start(mic_source="phone-microphone", source_lang=None)
        for _ in range(10):
            await session.on_audio(_voice_chunk())
        db["down"] = True
        await session.close()
        await asyncio.sleep(0.05)
        conv = get_conversation_store().get("default", "conv-key")
        assert conv is not None and conv.status == "live"

        db["down"] = False
        conv = await _until_ready("conv-key")
        assert conv.audio_key is not None

    asyncio.run(run())


def test_translation_of_a_held_turn_is_kept_and_does_not_fail_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The translation write used to raise out of the translation worker and then
    close() (a 1013 on session.end), or update a segment not yet stored."""
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
    monkeypatch.setattr(settings, "translation_backend", "stub")
    db = _outage(monkeypatch)

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, session_id="conv-es")
        await session.start(mic_source="phone-microphone", source_lang=None)
        db["down"] = True
        final = CaptionFinal(
            type="caption.final",
            segmentId="seg-es",
            text="hola, ¿qué tal?",
            startMs=0,
            endMs=2000,
            lang="es",
        )
        session._store(
            get_conversation_store().add_segment,
            Segment(segment_id="seg-es", text=final.text, start_ms=0, end_ms=2000, lang="es"),
        )
        session._consider_translation(final)
        assert session._translation_queue is not None
        await session._translation_queue.join()
        await session.close()  # must not raise

        db["down"] = False
        conv = await _until_ready("conv-es")
        assert [s.segment_id for s in conv.segments] == ["seg-es"]
        assert conv.segments[0].translation and "aloh" in conv.segments[0].translation

    asyncio.run(run())


def test_a_non_outage_write_error_drops_only_that_write() -> None:
    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, session_id="conv-fault")
        await session.start(mic_source="phone-microphone", source_lang=None)
        store = get_conversation_store()

        def broken(*_a, **_k):
            raise ValueError("not an outage")

        session._store(broken)
        session._store(store.add_segment, Segment(segment_id="s", text="t", start_ms=0, end_ms=1))
        await session.close()
        conv = store.get("default", "conv-fault")
        assert conv is not None and conv.status == "ready"
        assert [s.segment_id for s in conv.segments] == ["s"]

    asyncio.run(run())
