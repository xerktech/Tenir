"""SqlConversationStore.list/search against a REAL Postgres (XERK-1518).

Both used to read the page's ids and then call get() per row — its own pool borrow
plus three child reads each — so one GET /conversations queued 1+N times for the
4-connection pool. They now read the page and every child table in one borrow; these
tests pin that, and that batching never hands one conversation another's children.

Skipped unless ``TENIR_TEST_PG_DSN`` points at a disposable Postgres (CI provides one,
see test_pg_schema_live.py). Each test works in its own throwaway schema.
"""

from __future__ import annotations

import os
import uuid

import pytest

from api.persistence.models import Cue, Segment, Song

DSN = os.environ.get("TENIR_TEST_PG_DSN", "")
psycopg = pytest.importorskip("psycopg") if DSN else None

pytestmark = pytest.mark.skipif(not DSN, reason="TENIR_TEST_PG_DSN not set (needs a real Postgres)")

HH = "default"


@pytest.fixture
def store():
    """A store whose pool runs in a fresh, dropped-after schema, with borrows counted."""
    from psycopg.conninfo import make_conninfo

    from api.persistence.postgres import SqlConversationStore

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        s = SqlConversationStore(make_conninfo(DSN, options=f"-c search_path={schema}"))
        try:
            pool = s._ensure_pool()
            borrow = pool.connection
            s.borrows = 0

            def counting(*a, **k):
                s.borrows += 1
                return borrow(*a, **k)

            pool.connection = counting
            yield s
        finally:
            if s._pool is not None:
                s._pool.close()
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _seed(store, n: int, *, owner: str | None = None) -> list[str]:
    ids = []
    for i in range(n):
        cid = str(uuid.uuid4())
        store.create(HH, cid, owner=owner)
        # Inserted out of order so the per-conversation ordering is the query's, not luck.
        for j in (2, 0, 1):
            store.add_segment(
                HH,
                cid,
                Segment(
                    segment_id=f"{cid}-s{j}",
                    text=f"needle conv{i} seg{j}",
                    start_ms=j * 1000,
                    end_ms=j * 1000 + 500 + i,
                ),
            )
        # A tied start_ms: the batched read must order it like get() does (by id).
        store.add_segment(
            HH, cid, Segment(segment_id=f"{cid}-s1a", text="tie", start_ms=1000, end_ms=1100)
        )
        # Tied at_ms cues and songs, inserted against id order, for the same reason.
        for k in ("b", "a"):
            store.add_cue(HH, cid, Cue(cue_id=f"{cid}-c{k}", title=f"cue{i}", body="b", at_ms=i))
            store.add_song(
                HH, cid, Song(song_id=f"{cid}-g{k}", title=f"song{i}", artist="a", at_ms=i)
            )
        ids.append(cid)
    return ids


def _assert_matches_get(store, convs) -> None:
    for c in convs:
        assert c == store.get(HH, c.id)
        assert [s.segment_id[len(c.id) :] for s in c.segments] == ["-s0", "-s1", "-s1a", "-s2"]
        assert all(s.segment_id.startswith(c.id) for s in c.segments)
        assert [x.cue_id for x in c.cues] == [f"{c.id}-ca", f"{c.id}-cb"]
        assert [x.song_id for x in c.songs] == [f"{c.id}-ga", f"{c.id}-gb"]


def test_list_reads_the_page_in_one_borrow(store) -> None:
    ids = _seed(store, 12)

    store.borrows = 0
    convs = store.list(HH)
    assert store.borrows == 1
    # Newest first, every conversation, each with exactly its own children.
    assert [c.id for c in convs] == list(reversed(ids))
    _assert_matches_get(store, convs)

    store.borrows = 0
    page = store.list(HH, limit=3, offset=2)
    assert store.borrows == 1
    assert [c.id for c in page] == list(reversed(ids))[2:5]


def test_search_reads_the_page_in_one_borrow(store) -> None:
    ids = _seed(store, 8)
    lone = str(uuid.uuid4())
    store.create(HH, lone)  # no segments → never matches

    store.borrows = 0
    convs = store.search(HH, "needle")
    assert store.borrows == 1
    assert [c.id for c in convs] == list(reversed(ids))
    _assert_matches_get(store, convs)

    store.borrows = 0
    assert [c.id for c in store.search(HH, "conv3")] == [ids[3]]
    assert store.borrows == 1


def test_empty_page_and_owner_scope(store) -> None:
    store.borrows = 0
    assert store.list(HH) == []
    assert store.search(HH, "needle") == []
    assert store.borrows == 2

    with store._pool.connection() as conn:
        owner = str(
            conn.execute(
                "INSERT INTO users (username, password_hash, household) "
                "VALUES ('m', 'x', %s) RETURNING id",
                (HH,),
            ).fetchone()[0]
        )
    mine = _seed(store, 2, owner=owner)
    _seed(store, 2)  # unowned rows a member must never see
    assert {c.id for c in store.list(HH, owner=owner)} == set(mine)
    assert {c.id for c in store.search(HH, "needle", owner=owner)} == set(mine)
