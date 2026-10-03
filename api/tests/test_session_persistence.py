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


def test_close_persists_finals_still_queued_in_the_result_pump() -> None:
    """Regression (flaky CI on test_resumed_session_continues_the_segment_timeline):
    close() flushes the tail final into the transcriber's result queue and then
    flips its closed flag, but results() used to stop at the flag alone — a
    session closed while the pump was still catching up dropped every result
    left in the queue, losing finalized turns (the flush tail first) from the
    persisted transcript. Closing with the queue completely unpumped must still
    persist every final."""

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        session = Session(send, session_id="conv-drain")
        await session.start(mic_source="phone-microphone", source_lang=None)
        for _ in range(30):  # ~3s -> stub finals at [0,2000] + flush tail [2000,3000]
            await session.on_audio(_voice_chunk())
        # No drain sleeps: close immediately, with the whole result queue still
        # sitting unpumped. close() must drain it, not drop it.
        await session.close()

        conv = get_conversation_store().get("default", "conv-drain")
        assert conv is not None
        assert conv.segments, "finals queued at close() were dropped, not persisted"
        assert max(s.end_ms for s in conv.segments) == 3000
        assert [s.start_ms for s in conv.segments] == [0, 2000]

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


def test_resumed_session_continues_the_segment_timeline() -> None:
    """Regression: the second leg of a resumed conversation used to restart its
    segment timeline at 0 while its audio was appended *after* the first leg's in
    the retained WAV. The merged transcript then interleaved the two sittings
    when ordered by start_ms, and History playback (and the STT eval replay) read
    the wrong audio for every second-leg segment — the damage documented in
    scripts/stt_eval/RESULTS-2026-07.md. Second-leg segments must continue from
    the retained duration."""

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        leg1 = Session(send, session_id="conv-timeline")
        await leg1.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(30):  # ~3s of audio -> stub finals at [0,2000] + flush tail
            await leg1.on_audio(_voice_chunk())
        for _ in range(10):
            await asyncio.sleep(0)
        await leg1.close()

        convs = get_conversation_store()
        # Snapshot, not the store's live list — leg2 appends into that same list.
        first_leg = list(convs.get("default", "conv-timeline").segments)
        assert first_leg, "expected first-leg segments"
        first_end = max(s.end_ms for s in first_leg)
        retained_ms = (
            len(wav_to_pcm16(get_audio_store().get(audio_key("default", "conv-timeline"))))
            * 1000
            // 32000
        )

        leg2 = Session(send, session_id="conv-timeline")
        await leg2.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(30):
            await leg2.on_audio(_voice_chunk())
        for _ in range(10):
            await asyncio.sleep(0)
        await leg2.close()

        segments = convs.get("default", "conv-timeline").segments
        second_leg = segments[len(first_leg) :]
        assert second_leg, "expected second-leg segments"
        # Every second-leg segment sits after the whole retained first leg on the
        # conversation timeline — audio-aligned, not restarted at 0.
        assert min(s.start_ms for s in second_leg) == retained_ms
        assert retained_ms >= first_end
        # And the merged transcript is ordered without interleaving: sorting by
        # start_ms keeps the persisted (chronological) order.
        starts = [s.start_ms for s in segments]
        assert starts == sorted(starts)
        # The full timeline spans the whole retained recording.
        total_ms = (
            len(wav_to_pcm16(get_audio_store().get(audio_key("default", "conv-timeline"))))
            * 1000
            // 32000
        )
        assert max(s.end_ms for s in segments) == total_ms

    asyncio.run(run())


def test_resume_offset_falls_back_to_segment_end_without_retained_audio() -> None:
    """With no retained audio (audio backend off, or a memory backend emptied by
    a restart) the resumed timeline continues from the last persisted segment's
    end — the transcript stays monotonic even though audio alignment is gone."""

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        leg1 = Session(send, session_id="conv-no-audio")
        leg1._audio_store = None
        await leg1.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(30):
            await leg1.on_audio(_voice_chunk())
        for _ in range(10):
            await asyncio.sleep(0)
        await leg1.close()

        first_end = max(
            s.end_ms for s in get_conversation_store().get("default", "conv-no-audio").segments
        )
        assert first_end > 0

        leg2 = Session(send, session_id="conv-no-audio")
        leg2._audio_store = None
        assert await leg2._resume_offset_ms() == first_end

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


def _hold_flush(session: Session) -> asyncio.Event:
    """Stall `session`'s STT flush until the returned event is set — the shape of a
    teardown flushing against an STT outage (up to its 15 s cap)."""
    release = asyncio.Event()
    transcriber = session._transcriber
    real_flush = transcriber.flush

    async def held_flush() -> None:
        await release.wait()
        await real_flush()

    transcriber.flush = held_flush
    return release


def test_resume_during_prior_teardown_continues_its_timeline() -> None:
    """Regression (XERK-1500): a resume arriving after the grace window but while
    the old sitting is still closing read the offset before that sitting's flushed
    tail (and audio) was stored, so the new sitting's segments overlapped the old
    ones. It must continue from the closing sitting's end, without waiting on its
    teardown (a stalled flush must not hold the new sitting's session.ready)."""

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        leg1 = Session(send, session_id="conv-closing")
        leg1._audio_store = None  # offset would come from the segments alone
        await leg1.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(25):  # ~2.5s -> stub final at [0,2000], tail [2000,2500] on flush
            await leg1.on_audio(_voice_chunk())
        for _ in range(10):
            await asyncio.sleep(0)
        release = _hold_flush(leg1)
        closing = asyncio.create_task(leg1.close())  # the grace close, unregistered
        await asyncio.sleep(0)

        leg2 = Session(send, session_id="conv-closing")
        leg2._audio_store = None
        await asyncio.wait_for(leg2.start(mic_source="g2-microphone", source_lang=None), 1)
        assert not closing.done(), "the old sitting must still be flushing"
        assert leg2._start_offset_ms == 2500

        release.set()
        await closing
        first_end = max(
            s.end_ms for s in get_conversation_store().get("default", "conv-closing").segments
        )
        assert first_end == 2500
        await leg2.close()

    asyncio.run(run())


def test_resume_during_prior_teardown_keeps_audio_in_order() -> None:
    """The resumed sitting's audio is appended after the closing sitting's, even
    if it closes before that sitting's retain has run, and its segments line up
    with where its audio lands in the stored recording (XERK-1500)."""

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        key = audio_key("default", "conv-order")
        leg1 = Session(send, session_id="conv-order")
        await leg1.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(25):
            await leg1.on_audio(_voice_chunk(freq=200))
        _hold_flush(leg1)
        closing = asyncio.create_task(leg1.close())  # never released: flush times out
        await asyncio.sleep(0)  # close() has run up to its shielded teardown
        leg2 = Session(send, session_id="conv-order")
        # Resume before leg1's teardown (or its retain) has taken a single step.
        assert not leg1._first_retain.done()
        await leg2.start(mic_source="g2-microphone", source_lang=None)
        assert leg2._start_offset_ms == 2500
        for _ in range(10):
            await leg2.on_audio(_voice_chunk(freq=400))
        await leg2.close()

        pcm = wav_to_pcm16(get_audio_store().get(key))
        assert len(pcm) == 35 * 3200
        assert pcm[: 25 * 3200] == _voice_chunk(freq=200) * 25
        assert pcm[25 * 3200 :] == _voice_chunk(freq=400) * 10
        closing.cancel()

    asyncio.run(run())


def test_closing_registry_is_per_household_and_cleared() -> None:
    """A closing sitting only feeds the offset of a resume of the same household's
    conversation, and is forgotten once its teardown completes."""
    from api import session as session_mod

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        leg1 = Session(send, session_id="conv-shared-id", household="hh-a")
        await leg1.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(25):
            await leg1.on_audio(_voice_chunk())
        release = _hold_flush(leg1)
        closing = asyncio.create_task(leg1.close())
        await asyncio.sleep(0)

        other = Session(send, session_id="conv-shared-id", household="hh-b")
        assert await other._resume_offset_ms() == 0

        release.set()
        await closing
        assert ("hh-a", "conv-shared-id") not in session_mod._closing

    asyncio.run(run())


def test_resume_during_prior_teardown_keeps_order_when_prior_retain_fails() -> None:
    """If the closing sitting's first retain fails, its audio is only retried by
    its final _persist; the resumed sitting must not store ahead of that retry,
    or the recording plays the sittings in the wrong order (XERK-1500)."""

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        store = get_audio_store()
        real_put = store.put
        fails = [1]

        def flaky_put(key, wav) -> None:
            if fails:
                fails.pop()
                raise OSError("disk hiccup")
            real_put(key, wav)

        store.put = flaky_put
        try:
            leg1 = Session(send, session_id="conv-retry")
            await leg1.start(mic_source="g2-microphone", source_lang=None)
            for _ in range(25):
                await leg1.on_audio(_voice_chunk(freq=200))
            release = _hold_flush(leg1)
            closing = asyncio.create_task(leg1.close())
            await asyncio.sleep(0)

            leg2 = Session(send, session_id="conv-retry")
            await leg2.start(mic_source="g2-microphone", source_lang=None)
            for _ in range(10):
                await leg2.on_audio(_voice_chunk(freq=400))
            closing2 = asyncio.create_task(leg2.close())
            for _ in range(20):
                await asyncio.sleep(0)
            # leg2's own flush isn't held behind leg1's stalled teardown: its tail
            # final is already stored, so a shutdown deadline can't drop it.
            conv = get_conversation_store().get("default", "conv-retry")
            assert max(s.end_ms for s in conv.segments) == 3500
            release.set()
            await closing
            await closing2
        finally:
            store.put = real_put

        pcm = wav_to_pcm16(store.get(audio_key("default", "conv-retry")))
        assert pcm == _voice_chunk(freq=200) * 25 + _voice_chunk(freq=400) * 10

    asyncio.run(run())


def test_earlier_teardown_finishing_keeps_a_later_closing_sitting() -> None:
    """A chain of sittings: when the first one's teardown ends it must forget only
    itself, not the second sitting that is now closing — a third resume during
    that teardown still continues from the second sitting's end (XERK-1500)."""

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        leg1 = Session(send, session_id="conv-chain")
        leg1._audio_store = None  # the stores lag the closing sitting's end
        await leg1.start(mic_source="g2-microphone", source_lang=None)
        for _ in range(10):
            await leg1.on_audio(_voice_chunk())
        release1 = _hold_flush(leg1)
        closing1 = asyncio.create_task(leg1.close())
        await asyncio.sleep(0)

        leg2 = Session(send, session_id="conv-chain")
        leg2._audio_store = None  # the stores lag the closing sitting's end
        await leg2.start(mic_source="g2-microphone", source_lang=None)
        assert leg2._start_offset_ms == 1000
        for _ in range(10):
            await leg2.on_audio(_voice_chunk())
        release2 = _hold_flush(leg2)
        closing2 = asyncio.create_task(leg2.close())
        await asyncio.sleep(0)

        release1.set()
        await closing1  # leg1's teardown ends while leg2's is still flushing

        leg3 = Session(send, session_id="conv-chain")
        leg3._audio_store = None  # the stores lag the closing sitting's end
        assert await leg3._resume_offset_ms() == 2000
        release2.set()
        await closing2

    asyncio.run(run())
