"""SqlConversationStore.import_conversation against a REAL Postgres (XERK-2958).

A re-caption (``api.reprocess``) writes its copy in one transaction: a failure part-way must
leave no row, since the stale sweep would otherwise finish a short copy into history (QA).

Skipped unless ``TENIR_TEST_PG_DSN`` points at a disposable Postgres. Each test works in its
own throwaway schema.
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


def _kw(segments: list[Segment]) -> dict:
    return {
        "owner": "alice",
        "mic_source": "phone-microphone",
        "source_lang": "es",
        "segments": segments,
        "audio_key": "default/x.wav",
    }


def test_an_imported_copy_is_ready_with_every_segment(store) -> None:
    cid = str(uuid.uuid4())
    segs = [
        Segment(str(uuid.uuid4()), "hola", 0, 900, "es", "hello", "Speaker 1"),
        Segment(str(uuid.uuid4()), "hi", 900, 1500, "en", None, "Speaker 2"),
    ]
    store.import_conversation("default", cid, **_kw(segs))
    got = store.get("default", cid)
    assert got.status == "ready" and got.ended_at is not None and got.owner == "alice"
    assert got.audio_key == "default/x.wav"
    assert [(s.text, s.translation, s.speaker) for s in got.segments] == [
        ("hola", "hello", "Speaker 1"),
        ("hi", None, "Speaker 2"),
    ]
    assert cid in [c.id for c in store.list("default", owner="alice")]


def test_a_failure_part_way_leaves_nothing(store) -> None:
    cid = str(uuid.uuid4())
    dup = str(uuid.uuid4())
    segs = [
        Segment(dup, "uno", 0, 900, "es"),
        Segment(str(uuid.uuid4()), "dos", 900, 1800, "es"),
        Segment(dup, "tres", 1800, 2700, "es"),  # same id: the third INSERT fails
    ]
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.import_conversation("default", cid, **_kw(segs))
    assert store.get("default", cid) is None
    assert cid not in [c.id for c in store.list("default")]
