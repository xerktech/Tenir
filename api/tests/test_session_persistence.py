"""Session persistence — segments and full audio stored on end."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from api.persistence import audio_key, get_audio_store, get_conversation_store, wav_to_pcm16
from api.persistence.audio import InMemoryAudioStore
from api.persistence.conversations import InMemoryConversationStore
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


def test_session_persists_transcript_and_audio() -> None:
    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, session_id="conv-1")
        await session.start(mic_source="phone-microphone", source_lang=None)
        for _ in range(30):  # ~3s of voiced audio -> at least one finalized turn
            await session.on_audio(_voice_chunk())
        for _ in range(10):  # let the result pump drain
            await asyncio.sleep(0)
        await session.close()

        convs = get_conversation_store()
        conv = convs.get("default", "conv-1")
        assert conv is not None
        assert conv.mic_source == "phone-microphone"
        assert conv.segments, "expected persisted transcript segments"
        assert conv.status == "ready" and conv.ended_at is not None

        # Full audio was retained in the audio store and decodes.
        key = audio_key("default", "conv-1")
        wav = get_audio_store().get(key)
        assert wav is not None and wav_to_pcm16(wav)

    asyncio.run(run())


def test_session_without_persistence_does_not_retain() -> None:
    # Disable persistence on an already-constructed session by clearing its seams.
    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, session_id="conv-2")
        session._conversations = None
        session._audio_store = None
        await session.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(30):
            await session.on_audio(_voice_chunk())
        for _ in range(10):
            await asyncio.sleep(0)
        await session.close()

        assert get_conversation_store().get("default", "conv-2") is None

    asyncio.run(run())


def test_transcript_survives_a_client_that_disconnects_on_end() -> None:
    """The web client sends session.end and closes the socket immediately, so the
    end-of-session flush produces finals with nowhere to send them. A failing send
    used to kill the result pump before it persisted them, leaving the recorded
    session with an empty transcript (XERK-58)."""

    async def run() -> None:
        sent = 0

        async def send(_msg) -> None:
            nonlocal sent
            sent += 1
            if sent > 1:  # the client vanished after the first frame
                raise WebSocketDisconnect(code=1006)

        session = Session(send, session_id="conv-gone")
        await session.start(mic_source="phone-microphone", source_lang=None)
        for _ in range(30):  # ~3s of voiced audio -> at least one finalized turn
            await session.on_audio(_voice_chunk())
        for _ in range(10):  # let the result pump drain
            await asyncio.sleep(0)
        await session.close()  # flush -> finals nobody can receive

        conv = get_conversation_store().get("default", "conv-gone")
        assert conv is not None
        assert conv.segments, "captions must be persisted even when delivery fails"
        assert conv.status == "ready"

    asyncio.run(run())
