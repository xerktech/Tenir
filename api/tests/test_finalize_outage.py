"""A session.end during a database outage still finalizes the recording (XERK-1531)."""

from __future__ import annotations

import asyncio
import time

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


class _StatementTimeout(Exception):
    """psycopg's QueryCanceled as the server's statement_timeout raises it: a write
    blocked on a lock past STATEMENT_TIMEOUT_SECONDS (XERK-1513)."""

    sqlstate = "57014"


# Both must hold the write for retry rather than drop it.
_HELD = pytest.mark.parametrize(
    "error",
    [lambda: DatabaseUnavailable("database unavailable"), lambda: _StatementTimeout("canceled")],
    ids=["outage", "statement-timeout"],
)


def _outage(
    monkeypatch: pytest.MonkeyPatch,
    *names: str,
    wait_s: float = 0.0,
    error=lambda: DatabaseUnavailable("database unavailable"),
) -> dict[str, bool]:
    """Make conversation writes (default: all of them) raise ``error()`` (a Postgres
    outage) while ``down``, after ``wait_s`` (the pool timeout). ``calls`` counts
    attempts."""
    state = {"down": False, "calls": 0}
    store = get_conversation_store()
    for name in names or _WRITES:
        real = getattr(store, name)

        def call(*args, _real=real, **kwargs):
            if state["down"]:
                state["calls"] += 1
                time.sleep(wait_s)
                raise error()
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


@_HELD
def test_end_during_outage_finalizes_once_the_database_is_back(
    monkeypatch: pytest.MonkeyPatch, error
) -> None:
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
    db = _outage(monkeypatch, error=error)

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

        attempts = db["calls"]
        await asyncio.sleep(0.1)  # several retries while still down
        conv = get_conversation_store().get("default", "conv-outage")
        assert conv is not None and conv.status == "live"
        # Paced by the retry interval (0.01 s), not a hot loop.
        assert 2 <= db["calls"] - attempts <= 25

        db["down"] = False
        conv = await _until_ready("conv-outage")
        assert conv.status == "ready" and conv.ended_at is not None
        # Every turn recorded before the end survived the outage, tail included.
        assert [s.start_ms for s in conv.segments] == [0, 2000]
        assert conv.audio_key is not None

    asyncio.run(run())


@_HELD
def test_outage_mid_session_keeps_captions_and_stores_held_turns_later(
    monkeypatch: pytest.MonkeyPatch, error
) -> None:
    """A failed segment write used to kill the result pump: captions stopped for the
    rest of the session and every later turn was lost. Now the turn is held and
    stored with the next one once the database is back."""
    db = _outage(monkeypatch, error=error)

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


@_HELD
def test_finalize_waits_for_an_audio_key_the_outage_dropped(
    monkeypatch: pytest.MonkeyPatch, error
) -> None:
    """Only set_audio_key fails: finish() must not mark the row ready without the key
    to its stored WAV, or History can't play it."""
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
    db = _outage(monkeypatch, "set_audio_key", error=error)

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


def test_outage_writes_never_hold_the_caption_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each write waits out the pool timeout during an outage; awaited on the pump,
    that held every later caption until it went stale."""
    db = _outage(monkeypatch, wait_s=0.5)

    async def run() -> None:
        captions: list[str] = []

        async def send(msg) -> None:
            if msg.type == "caption.final":
                captions.append(msg.segmentId)

        session = Session(send, session_id="conv-live")
        await session.start(mic_source="phone-microphone", source_lang=None)
        db["down"] = True
        for _ in range(50):  # ~5s -> stub finals [0,2000] and [2000,4000]
            await session.on_audio(_voice_chunk())
        for _ in range(50):
            if len(captions) == 2:
                break
            await asyncio.sleep(0.002)
        assert len(captions) == 2, "a held write blocked the pump"
        db["down"] = False
        await session.close()

    asyncio.run(run())


def test_a_non_outage_audio_key_failure_does_not_stall_other_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
    store = get_conversation_store()
    real_key = store.set_audio_key

    def key(household, conversation_id, audio_key):
        if conversation_id == "conv-badkey":
            raise ValueError("disk full")
        return real_key(household, conversation_id, audio_key)

    monkeypatch.setattr(store, "set_audio_key", key)
    db = _outage(monkeypatch, "finish")

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        bad = Session(send, session_id="conv-badkey")
        await bad.start(mic_source="phone-microphone", source_lang=None)
        await bad.on_audio(_voice_chunk())
        db["down"] = True
        await bad.close()  # finish deferred by the outage; its key never writes
        other = Session(send, session_id="conv-other")
        await other.start(mic_source="phone-microphone", source_lang=None)
        await other.close()

        db["down"] = False
        assert (await _until_ready("conv-badkey")).audio_key is None
        assert (await _until_ready("conv-other")).status == "ready"

    asyncio.run(run())


def test_a_deferred_finalize_does_not_close_a_cold_resumed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (XERK-1502, QA): a sitting that ended during an outage finishes
    the row from the retry loop, after its teardown left the resume handoff. A cold
    resume landing before that retry reopens the row, and the retry must leave it
    live for the new sitting, which finishes it itself."""
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.05)
    db = _outage(monkeypatch, "finish")

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        first = Session(send, session_id="conv-deferred-resume")
        await first.start(mic_source="phone-microphone", source_lang=None)
        db["down"] = True
        await first.close()
        assert first in session_mod._unfinalized
        assert not session_mod._closing  # its teardown is done; only the retry is left
        db["down"] = False

        resumed = Session(send, session_id="conv-deferred-resume")
        await resumed.start(mic_source="phone-microphone", source_lang=None)
        for _ in range(100):  # the retry runs and drains the deferred finalize
            if not session_mod._unfinalized:
                break
            await asyncio.sleep(0.01)
        assert not session_mod._unfinalized
        conv = get_conversation_store().get("default", "conv-deferred-resume")
        assert conv is not None and conv.status == "live" and conv.ended_at is None

        await resumed.close()
        conv = await _until_ready("conv-deferred-resume")
        assert conv.ended_at is not None

    asyncio.run(run())


def test_a_resume_in_another_household_does_not_hold_a_deferred_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferred-finalize link is per (household, id): a colliding id in another
    household must not leave this household's row live until the next boot."""
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.05)
    db = _outage(monkeypatch, "finish")

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        first = Session(send, session_id="conv-shared-id", household="h1")
        await first.start(mic_source="phone-microphone", source_lang=None)
        db["down"] = True
        await first.close()
        db["down"] = False

        other = Session(send, session_id="conv-shared-id", household="h2")
        await other.start(mic_source="phone-microphone", source_lang=None)
        assert first._successor is None
        for _ in range(100):
            conv = get_conversation_store().get("h1", "conv-shared-id")
            if conv is not None and conv.status == "ready":
                break
            await asyncio.sleep(0.01)
        assert get_conversation_store().get("h1", "conv-shared-id").status == "ready"
        assert get_conversation_store().get("h2", "conv-shared-id").status == "live"
        await other.close()

    asyncio.run(run())


def test_a_finish_blocked_on_its_own_row_does_not_stall_other_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one session's finish() times out (57014: a lock on its row). It stays
    held for retry, not dropped, and the sessions queued behind it still finalize —
    an outage blocks them all alike, but a row lock does not (XERK-1513 QA)."""
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
    store = get_conversation_store()
    real_finish = store.finish
    locked = {"conv-locked"}

    def finish(household, conversation_id, **kwargs):
        if conversation_id in locked:
            raise _StatementTimeout("canceling statement due to statement timeout")
        return real_finish(household, conversation_id, **kwargs)

    monkeypatch.setattr(store, "finish", finish)

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        first = Session(send, session_id="conv-locked")
        await first.start(mic_source="phone-microphone", source_lang=None)
        await first.on_audio(_voice_chunk())
        await first.close()  # must not raise; finish deferred
        other = Session(send, session_id="conv-free")
        await other.start(mic_source="phone-microphone", source_lang=None)
        db = _outage(monkeypatch, "finish")
        db["down"] = True
        await other.close()  # queued behind the locked one
        db["down"] = False
        monkeypatch.setattr(store, "finish", finish)

        assert (await _until_ready("conv-free")).status == "ready"
        conv = store.get("default", "conv-locked")
        assert conv is not None and conv.status == "live"
        assert first in session_mod._unfinalized

        locked.clear()  # the lock is released
        assert (await _until_ready("conv-locked")).audio_key is not None

    asyncio.run(run())


def test_an_audio_key_blocked_on_its_own_row_does_not_stall_other_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retain path: one session's set_audio_key times out on its row lock (57014).
    Its finalize waits for the key; the sessions behind it still finalize."""
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
    store = get_conversation_store()
    real_key = store.set_audio_key
    locked = {"conv-keylocked"}

    def key(household, conversation_id, audio_key):
        if conversation_id in locked:
            raise _StatementTimeout("canceling statement due to statement timeout")
        return real_key(household, conversation_id, audio_key)

    monkeypatch.setattr(store, "set_audio_key", key)
    real_finish = store.finish
    down = {"finish": True}

    def finish(household, conversation_id, **kwargs):
        if down["finish"]:
            raise DatabaseUnavailable("database unavailable")
        return real_finish(household, conversation_id, **kwargs)

    monkeypatch.setattr(store, "finish", finish)

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        first = Session(send, session_id="conv-keylocked")
        await first.start(mic_source="phone-microphone", source_lang=None)
        await first.on_audio(_voice_chunk())
        await first.close()
        other = Session(send, session_id="conv-keyfree")
        await other.start(mic_source="phone-microphone", source_lang=None)
        await other.on_audio(_voice_chunk())
        await other.close()  # deferred by the outage, behind the locked one
        down["finish"] = False

        assert (await _until_ready("conv-keyfree")).audio_key is not None
        conv = store.get("default", "conv-keylocked")
        assert conv is not None and conv.status == "live"

        locked.clear()
        assert (await _until_ready("conv-keylocked")).audio_key is not None

    asyncio.run(run())


def test_an_outage_costs_one_attempt_per_pass_not_one_per_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During a whole-database outage the retry pass stops at the head session: each
    attempt waits out the pool, so walking the queue would tie up the one executor
    thread for N waits per pass."""
    monkeypatch.setattr(session_mod, "_FINALIZE_RETRY_S", 0.01)
    store = get_conversation_store()
    real_finish = store.finish
    state = {"down": True}
    attempts: list[str] = []

    def finish(household, conversation_id, **kwargs):
        if state["down"]:
            attempts.append(conversation_id)
            raise DatabaseUnavailable("database unavailable")
        return real_finish(household, conversation_id, **kwargs)

    monkeypatch.setattr(store, "finish", finish)

    async def run() -> None:
        async def send(_msg) -> None:
            pass

        for name in ("conv-o1", "conv-o2", "conv-o3"):
            s = Session(send, session_id=name)
            await s.start(mic_source="phone-microphone", source_lang=None)
            await s.close()
        attempts.clear()
        await asyncio.sleep(0.1)  # several passes, still down
        assert attempts and set(attempts) == {"conv-o1"}, attempts

        state["down"] = False
        for name in ("conv-o1", "conv-o2", "conv-o3"):
            await _until_ready(name)

    asyncio.run(run())
