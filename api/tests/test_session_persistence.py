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


def test_resumed_session_extends_retained_audio_across_the_grace_window() -> None:
    """A session that resumes *after* the resume grace window has expired reaches
    the api as a brand-new ``Session`` object bound to the *same* conversation id
    (the glasses persist their session id across drops and relaunches, so this is
    their normal lifecycle — not an edge case). Its full-audio buffer starts empty
    and only holds the post-resume portion. Retaining that alone would overwrite
    the earlier audio, leaving the stored conversation with a fragment that no
    longer matches its transcript. The retained audio must span the whole
    conversation, so the web UI can replay every glasses session end to end
    (XERK-86)."""

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        # First leg: audio at 200 Hz, persisted when the socket drops and the grace
        # window lapses (a new Session, close()d by the grace-close path).
        leg1 = Session(send, session_id="conv-resumed")
        await leg1.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(30):
            await leg1.on_audio(_voice_chunk(freq=200))
        for _ in range(10):
            await asyncio.sleep(0)
        await leg1.close()

        first = get_audio_store().get(audio_key("default", "conv-resumed"))
        assert first is not None
        first_samples = len(wav_to_pcm16(first)) // 2

        # Second leg: the client reconnects with the same id after grace expiry, so
        # the api starts a fresh Session on the existing conversation. Distinct tone
        # (400 Hz) so we can tell the legs apart in the retained audio.
        leg2 = Session(send, session_id="conv-resumed")
        await leg2.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(30):
            await leg2.on_audio(_voice_chunk(freq=400))
        for _ in range(10):
            await asyncio.sleep(0)
        await leg2.close()

        # The retained audio must now cover both legs, not just the last one.
        combined = get_audio_store().get(audio_key("default", "conv-resumed"))
        assert combined is not None
        combined_samples = len(wav_to_pcm16(combined)) // 2
        assert combined_samples > first_samples, (
            "resumed session overwrote earlier audio instead of extending it — "
            f"stored {combined_samples} samples, first leg alone had {first_samples}"
        )
        # Both legs are the same length, so the extended clip is exactly their sum.
        assert combined_samples == first_samples * 2

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
