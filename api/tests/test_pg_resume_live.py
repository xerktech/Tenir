"""SqlConversationStore.create on a resume, against a REAL Postgres (XERK-1502).

A cold resume of a finished conversation calls create() on an existing row. It
used to be ON CONFLICT DO NOTHING, so the row stayed 'ready' with the first
sitting's ended_at while the new sitting recorded into it. It must reopen the row
(live, no ended_at) and keep the original owner and start, and never touch another
household's row on an id collision.

Skipped unless ``TENIR_TEST_PG_DSN`` points at a disposable Postgres (CI provides one,
see test_pg_schema_live.py). Each test works in its own throwaway schema.
"""

from __future__ import annotations

import os
import uuid

import pytest

DSN = os.environ.get("TENIR_TEST_PG_DSN", "")
psycopg = pytest.importorskip("psycopg") if DSN else None

pytestmark = pytest.mark.skipif(not DSN, reason="TENIR_TEST_PG_DSN not set (needs a real Postgres)")


@pytest.fixture
def store():
    from psycopg.conninfo import make_conninfo

    from api.persistence.postgres import SqlConversationStore

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        s = SqlConversationStore(make_conninfo(DSN, options=f"-c search_path={schema}"))
        try:
            yield s
        finally:
            if s._pool is not None:
                s._pool.close()
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def test_create_on_a_finished_row_reopens_it_keeping_owner_and_start(store) -> None:
    cid = str(uuid.uuid4())
    first = store.create("default", cid, owner="alice", mic_source="g2-microphone")
    store.finish("default", cid)
    assert store.get("default", cid).status == "ready"

    resumed = store.create("default", cid, owner="bob", mic_source="phone")
    assert resumed.status == "live"
    assert resumed.ended_at is None
    assert resumed.owner == "alice"
    assert resumed.mic_source == "g2-microphone"
    assert resumed.started_at == first.started_at

    done = store.finish("default", cid)
    assert done.status == "ready" and done.ended_at is not None


def test_create_reopens_a_row_the_stale_sweep_finalized(store) -> None:
    cid = str(uuid.uuid4())
    store.create("default", cid)
    assert store.finish_stale() == [("default", cid)]
    assert store.get("default", cid).status == "ready"
    assert store.create("default", cid).status == "live"


def test_create_never_reopens_another_households_row(store) -> None:
    cid = str(uuid.uuid4())
    with store._ensure_pool().connection() as conn:
        for hh in ("hh-a", "hh-b"):
            conn.execute("INSERT INTO households (id) VALUES (%s) ON CONFLICT DO NOTHING", (hh,))
    store.create("hh-a", cid, owner="alice")
    store.finish("hh-a", cid)
    with pytest.raises(AssertionError):
        store.create("hh-b", cid)  # the id is taken; nothing of hh-b's to return
    other = store.get("hh-a", cid)
    assert other.status == "ready" and other.ended_at is not None
    assert other.owner == "alice"


def test_set_audio_key_reports_whether_the_row_exists(store) -> None:
    """A live session drops its WAV when this finds no row: its recording was
    deleted while it was still recording (XERK-1608)."""
    cid = str(uuid.uuid4())
    assert store.set_audio_key("default", cid, f"default/{cid}.wav") is False
    store.create("default", cid)
    assert store.set_audio_key("default", cid, f"default/{cid}.wav") is True
    assert store.get("default", cid).audio_key == f"default/{cid}.wav"
    store.delete("default", cid)
    assert store.set_audio_key("default", cid, f"default/{cid}.wav") is False


def test_reopen_reopens_a_finished_row_and_never_inserts(store) -> None:
    """A cold resume reopens with an UPDATE only: a recording deleted since the
    resume's existence check must stay deleted (XERK-1591)."""
    cid = str(uuid.uuid4())
    assert store.reopen("default", cid) is None
    assert store.get("default", cid) is None
    first = store.create("default", cid, owner="alice")
    store.finish("default", cid)
    reopened = store.reopen("default", cid)
    assert reopened is not None and reopened.status == "live" and reopened.ended_at is None
    assert reopened.owner == "alice" and reopened.started_at == first.started_at
    store.delete("default", cid)
    assert store.reopen("default", cid) is None
    assert store.get("default", cid) is None


def test_reopen_never_touches_another_households_row(store) -> None:
    cid = str(uuid.uuid4())
    with store._ensure_pool().connection() as conn:
        for hh in ("hh-a", "hh-b"):
            conn.execute("INSERT INTO households (id) VALUES (%s) ON CONFLICT DO NOTHING", (hh,))
    store.create("hh-a", cid)
    store.finish("hh-a", cid)
    assert store.reopen("hh-b", cid) is None
    assert store.get("hh-a", cid).status == "ready"


def test_a_cold_resume_whose_recording_is_deleted_mid_start_does_not_recreate_it(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DELETE lands while start() reads the resume offset, after the handler's
    existence check: the start fails with RecordingGone and the row stays gone."""
    import asyncio

    from api.contract import MicSource
    from api.session import Session

    cid = str(uuid.uuid4())
    store.create("default", cid, owner="alice")
    store.finish("default", cid)

    async def deleted_meanwhile(self: Session) -> int:
        await asyncio.sleep(0.2)  # QA's widened window
        await asyncio.to_thread(store.delete, "default", cid)
        return 0

    monkeypatch.setattr(Session, "_resume_offset_ms", deleted_meanwhile)

    async def sent(_msg: object) -> None:
        pass

    async def run() -> None:
        session = Session(
            sent, session_id=cid, household="default", user_id="alice", reopen=True
        )
        session._conversations = store
        session._audio_store = None
        with pytest.raises(Exception, match=cid):  # RecordingGone
            await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)

    asyncio.run(run())
    assert store.get("default", cid) is None
