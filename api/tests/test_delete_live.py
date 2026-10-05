"""Deleting a recording while it is still live must not leave its WAV behind (XERK-1608).

The delete removes the row and any stored WAV, but the live session's teardown then
stores its audio again; its key write found no row and the WAV stayed on disk.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from api import session as session_mod
from api.main import app
from api.persistence import audio_key, get_audio_store, get_conversation_store
from api.session import Session

CHUNK = b"\x11\x22" * 1600


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    get_conversation_store()._by_household.clear()
    get_audio_store()._blobs.clear()
    monkeypatch.setattr(session_mod, "_STT_FLUSH_TIMEOUT_S", 0.05)


async def _live_session() -> Session:
    async def send(_msg) -> None:
        pass

    s = Session(send, household="default")
    await s.start(mic_source="phone-microphone", source_lang=None)
    await s.on_audio(CHUNK)
    return s


def test_deleting_a_live_recording_drops_the_audio_its_session_stores_later() -> None:
    async def run() -> None:
        s = await _live_session()
        r = await asyncio.to_thread(TestClient(app).delete, f"/conversations/{s.session_id}")
        assert r.status_code == 204
        await s.close()
        assert get_conversation_store().get("default", s.session_id) is None
        assert not get_audio_store().exists(audio_key("default", s.session_id))

    asyncio.run(run())


def test_a_delete_landing_between_storing_and_linking_still_drops_the_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_audio_store()
    real_put = store.put

    def put_then_deleted(key: str, data: bytes) -> None:
        real_put(key, data)
        hh, _, name = key.partition("/")
        get_conversation_store().delete(hh, name.removesuffix(".wav"))

    monkeypatch.setattr(store, "put", put_then_deleted)

    async def run() -> None:
        s = await _live_session()
        await s.close()
        assert not store.exists(audio_key("default", s.session_id))

    asyncio.run(run())


def test_a_recording_that_was_not_deleted_keeps_its_audio() -> None:
    async def run() -> None:
        s = await _live_session()
        await s.close()
        key = audio_key("default", s.session_id)
        assert get_audio_store().exists(key)
        assert get_conversation_store().get("default", s.session_id).audio_key == key

    asyncio.run(run())


def test_a_session_storing_and_linking_inside_the_delete_still_loses_its_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session stores and links its WAV after the endpoint deleted the audio but
    before it deleted the row: the endpoint's second pass must remove it."""
    store = get_conversation_store()
    real_delete = store.delete

    async def run() -> None:
        loop = asyncio.get_running_loop()
        s = await _live_session()

        def delete_after_the_session_closes(hh: str, cid: str) -> bool:
            asyncio.run_coroutine_threadsafe(s.close(), loop).result(10)
            assert store.get(hh, cid).audio_key == audio_key(hh, cid)
            return real_delete(hh, cid)

        monkeypatch.setattr(store, "delete", delete_after_the_session_closes)
        r = await asyncio.to_thread(TestClient(app).delete, f"/conversations/{s.session_id}")
        assert r.status_code == 204
        assert store.get("default", s.session_id) is None
        assert not get_audio_store().exists(audio_key("default", s.session_id))

    asyncio.run(run())


def test_a_failed_audio_delete_keeps_the_row_to_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> str:
        s = await _live_session()
        await s.close()
        return s.session_id

    sid = asyncio.run(run())

    def disk_error(_key: str) -> bool:
        raise OSError("disk")

    monkeypatch.setattr(get_audio_store(), "delete", disk_error)
    r = TestClient(app, raise_server_exceptions=False).delete(f"/conversations/{sid}")
    assert r.status_code == 500
    assert get_conversation_store().get("default", sid) is not None
