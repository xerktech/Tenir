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

from api.persistence.models import Segment

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


def test_stale_sweep_never_ends_a_resumed_recording_before_its_last_sitting(store) -> None:
    """XERK-1591: a resumed recording's segments are on its audio timeline, which
    leaves out the gap between sittings. The sweep's started_at + MAX(end_ms) put
    the crashed sitting's end yesterday, before the first sitting's own ended_at."""
    cid = str(uuid.uuid4())
    store.create("default", cid)
    store.add_segment("default", cid, Segment(f"{cid}-1", "first sitting", 0, 60_000))
    with store._ensure_pool().connection() as conn:
        # The first sitting started yesterday and finished just now.
        conn.execute(
            "UPDATE conversations SET started_at = now() - INTERVAL '1 day' WHERE id = %s",
            (cid,),
        )
    first_end = store.finish("default", cid).ended_at
    resumed = store.create("default", cid)  # a cold resume, then the process crashes
    store.add_segment("default", cid, Segment(f"{cid}-2", "second sitting", 60_000, 90_000))

    assert store.finish_stale() == [("default", cid)]
    swept = store.get("default", cid)
    assert swept.status == "ready"
    assert swept.ended_at >= first_end
    assert swept.ended_at >= resumed.opened_at


def test_stale_sweep_still_ends_a_single_sitting_at_its_last_segment(store) -> None:
    cid = str(uuid.uuid4())
    created = store.create("default", cid)
    store.add_segment("default", cid, Segment(f"{cid}-1", "words", 0, 90_000))
    store.finish_stale()
    swept = store.get("default", cid)
    assert (swept.ended_at - created.started_at).total_seconds() == 90


def test_a_late_finish_from_an_earlier_sitting_leaves_the_reopened_row_live(store) -> None:
    """XERK-1591: an earlier sitting's finish() that lands after a resume reopened
    the row must not flip it back to ready; the current sitting's finish closes it."""
    cid = str(uuid.uuid4())
    first = store.create("default", cid)
    second = store.create("default", cid)  # the resume commits first
    assert second.opened_at != first.opened_at

    late = store.finish("default", cid, opened_at=first.opened_at)
    assert late.status == "live" and late.ended_at is None

    done = store.finish("default", cid, opened_at=second.opened_at)
    assert done.status == "ready" and done.ended_at is not None


def test_create_returns_its_own_token_when_a_resume_reopens_before_the_read(store) -> None:
    """XERK-1591 QA: create() read its token back with a separate get(); a sitting
    reopening the row in between handed the earlier sitting the later one's token,
    so the earlier sitting's late finish closed the live row."""
    cid = str(uuid.uuid4())
    store.create("default", cid)
    real_get = store.get
    later = []

    def get(*args, **kwargs):
        store.get = real_get
        later.append(store.create("default", cid))  # another sitting's reopen
        return real_get(*args, **kwargs)

    store.get = get
    try:
        first = store.create("default", cid)
    finally:
        store.get = real_get
    assert first.opened_at != later[0].opened_at
    assert store.finish("default", cid, opened_at=first.opened_at).status == "live"
    assert store.finish("default", cid, opened_at=later[0].opened_at).status == "ready"


def test_reopen_stamps_its_own_token_so_an_earlier_late_finish_leaves_it_live(store) -> None:
    """XERK-1591: a cold resume reopens through reopen(), not create(). It must
    stamp a new sitting token and return it, or the earlier sitting's late
    finish() closes the reopened row (and the resumed sitting's own finish has
    no token)."""
    cid = str(uuid.uuid4())
    first = store.create("default", cid)
    reopened = store.reopen("default", cid)
    assert reopened.opened_at is not None and reopened.opened_at != first.opened_at
    assert store.finish("default", cid, opened_at=first.opened_at).status == "live"
    assert store.finish("default", cid, opened_at=reopened.opened_at).status == "ready"


def test_reopen_returns_its_own_token_when_another_reopen_lands_before_the_read(store) -> None:
    cid = str(uuid.uuid4())
    store.create("default", cid)
    real_get = store.get
    later = []

    def get(*args, **kwargs):
        store.get = real_get
        later.append(store.reopen("default", cid))  # another sitting's reopen
        return real_get(*args, **kwargs)

    store.get = get
    try:
        mine = store.reopen("default", cid)
    finally:
        store.get = real_get
    assert mine.opened_at != later[0].opened_at
    assert store.finish("default", cid, opened_at=mine.opened_at).status == "live"


def test_a_cold_resumes_reopen_survives_the_earlier_sittings_late_finish(store) -> None:
    """End to end on Postgres: sitting A's finish() is held after it passed
    _resumed_live; sitting B cold-resumes through reopen() meanwhile. A's late
    finish leaves B's row live, and B's own finish (its reopen token) closes it."""
    import asyncio
    import threading

    from api.contract import MicSource
    from api.session import Session

    cid = str(uuid.uuid4())
    entered, release = threading.Event(), threading.Event()
    real_finish = store.finish

    def held_finish(*args, **kwargs):
        if not release.is_set():
            entered.set()
            release.wait(10)
        return real_finish(*args, **kwargs)

    async def sent(_msg: object) -> None:
        pass

    def bound(session: Session) -> Session:
        session._conversations = store
        session._audio_store = None
        return session

    async def run() -> None:
        a = bound(Session(sent, session_id=cid, household="default", user_id="alice"))
        await a.start(mic_source=MicSource("phone-microphone"), source_lang=None)
        store.finish = held_finish
        try:
            closing = asyncio.create_task(a.close())
            assert await asyncio.to_thread(entered.wait, 10)
            b = bound(
                Session(sent, session_id=cid, household="default", user_id="alice", reopen=True)
            )
            await b.start(mic_source=MicSource("phone-microphone"), source_lang=None)
            release.set()
            await closing
        finally:
            release.set()
            store.finish = real_finish
        row = store.get("default", cid)
        assert row.status == "live" and row.ended_at is None
        await b.close()
        assert store.get("default", cid).status == "ready"

    asyncio.run(run())
