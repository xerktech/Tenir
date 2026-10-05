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
