"""Screen-meeting persistence (XERK-2505) against a REAL Postgres: the additive schema.sql
columns and tables, and the SqlConversationStore methods that write and read them.

Skipped unless ``TENIR_TEST_PG_DSN`` points at a disposable Postgres (see
test_pg_schema_live.py). Each test works in its own throwaway schema.
"""

from __future__ import annotations

import os
import uuid

import pytest

from api.persistence.models import ScreenMeta, Segment

DSN = os.environ.get("TENIR_TEST_PG_DSN", "")
psycopg = pytest.importorskip("psycopg") if DSN else None

pytestmark = pytest.mark.skipif(not DSN, reason="TENIR_TEST_PG_DSN not set (needs a real Postgres)")

HH = "default"


@pytest.fixture
def store():
    from psycopg.conninfo import make_conninfo

    from api.persistence.postgres import SqlConversationStore

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        s = SqlConversationStore(make_conninfo(DSN, options=f"-c search_path={schema}"))
        try:
            s._ensure_pool()
            yield s
        finally:
            if s._pool is not None:
                s._pool.close()
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def test_screen_meeting_round_trips(store) -> None:
    cid = str(uuid.uuid4())
    conv = store.create(HH, cid, owner="u1", screen=ScreenMeta("tesoro", "Tesoro", "zoom"))
    assert (conv.source, conv.org_site_key, conv.org_name, conv.platform) == (
        "screen",
        "tesoro",
        "Tesoro",
        "zoom",
    )
    store.set_meeting_title(HH, cid, "Q3 Release Sync")
    # Tiles seen in one frame share their ms: they keep tile order, not name order.
    store.add_participants(HH, cid, [("Priya Raman", 0), ("Sean O'Neil", 0)])
    store.add_participants(HH, cid, [("Priya Raman", 9000), ("Kunal", 700)])
    store.add_segment(HH, cid, Segment("s1", "hello", 0, 100, "en", speaker="Priya Raman"))
    store.finish(HH, cid)
    got = store.get(HH, cid)
    assert got.meeting_title == "Q3 Release Sync"
    assert got.participants == ["Priya Raman", "Sean O'Neil", "Kunal"]
    store.add_participants(HH, cid, [("Zed", 800), ("Abe", 800)])
    assert store.get(HH, cid).participants[-2:] == ["Zed", "Abe"]
    assert got.segments[0].speaker == "Priya Raman"
    (summary,) = store.list(HH)
    assert (summary.source, summary.org_name, summary.meeting_title) == (
        "screen",
        "Tesoro",
        "Q3 Release Sync",
    )
    # Participants of another household's id never land.
    store.add_participants("other", cid, [("Mallory", 0)])
    assert "Mallory" not in store.get(HH, cid).participants


def test_mic_conversations_default_to_source_mic(store) -> None:
    cid = str(uuid.uuid4())
    conv = store.create(HH, cid, owner="u1", mic_source="phone-microphone")
    assert conv.source == "mic" and conv.org_site_key is None and conv.participants == []
    store.add_segment(HH, cid, Segment("s2", "hi", 0, 10))
    assert store.get(HH, cid).segments[0].speaker is None


def test_speaker_aliases_are_per_org_and_case_insensitive(store) -> None:
    store.set_speaker_alias(HH, "u1", "tesoro", " Kunal@TesoroXP.com", "Kunal")
    store.set_speaker_alias(HH, "u1", "tesoro", "kunal@tesoroxp.com", "Kunal S.")
    assert store.speaker_aliases(HH, "u1", "tesoro") == {"kunal@tesoroxp.com": "Kunal S."}
    assert store.speaker_aliases(HH, "u1", "yprime") == {}


def test_speaker_aliases_are_per_owner(store) -> None:
    """XERK-2606: two members alias the same speaker independently; neither sees the other's."""
    store.set_speaker_alias(HH, "u1", "tesoro", "sam@example.test", "Sam Q")
    store.set_speaker_alias(HH, "u2", "tesoro", "sam@example.test", "Samuel")
    assert store.speaker_aliases(HH, "u1", "tesoro") == {"sam@example.test": "Sam Q"}
    assert store.speaker_aliases(HH, "u2", "tesoro") == {"sam@example.test": "Samuel"}
    assert store.speaker_aliases(HH, "u3", "tesoro") == {}


def test_schema_apply_is_idempotent_with_the_new_tables(store) -> None:
    from api.persistence.postgres import apply_boot_schema

    apply_boot_schema(store._ensure_pool())
    apply_boot_schema(store._ensure_pool())


def test_a_participants_table_without_position_is_migrated(store) -> None:
    """A database first set up before participants.position existed (QA delta)."""
    from api.persistence.postgres import apply_boot_schema

    pool = store._ensure_pool()
    with pool.connection() as conn:
        conn.execute("ALTER TABLE participants DROP COLUMN position")
    apply_boot_schema(pool)
    cid = str(uuid.uuid4())
    store.create(HH, cid, owner="u1", screen=ScreenMeta("tesoro", "Tesoro", "zoom"))
    store.add_participants(HH, cid, [("Priya Raman", 0)])
    assert store.get(HH, cid).participants == ["Priya Raman"]


def test_a_household_wide_speaker_aliases_table_is_migrated(store) -> None:
    """XERK-2606: a database set up with the XERK-2505 (household, org, alias) key. The boot
    apply moves it to per-owner keys: two members may alias one speaker, the old rows match
    no member, and a write without an owner is refused."""
    from api.persistence.postgres import apply_boot_schema

    pool = store._ensure_pool()
    with pool.connection() as conn:
        conn.execute("DROP TABLE speaker_aliases")
        conn.execute(
            """
            CREATE TABLE speaker_aliases (
                household    TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
                org_site_key TEXT NOT NULL,
                alias        TEXT NOT NULL,
                name         TEXT NOT NULL,
                PRIMARY KEY (household, org_site_key, alias)
            )
            """
        )
        conn.execute(
            "INSERT INTO speaker_aliases VALUES (%s, 'tesoro', 'kunal@tesoroxp.com', 'Old')",
            (HH,),
        )
    apply_boot_schema(pool)
    apply_boot_schema(pool)
    assert store.speaker_aliases(HH, "u1", "tesoro") == {}  # the old row belongs to no one
    store.set_speaker_alias(HH, "u1", "tesoro", "kunal@tesoroxp.com", "K1")
    store.set_speaker_alias(HH, "u2", "tesoro", "kunal@tesoroxp.com", "K2")
    assert store.speaker_aliases(HH, "u1", "tesoro") == {"kunal@tesoroxp.com": "K1"}
    assert store.speaker_aliases(HH, "u2", "tesoro") == {"kunal@tesoroxp.com": "K2"}
    assert store.speaker_aliases(HH, "u3", "tesoro") == {}
    with pytest.raises(psycopg.errors.NotNullViolation), pool.connection() as conn:
        conn.execute(
            "INSERT INTO speaker_aliases (household, org_site_key, alias, name)"
            " VALUES (%s, 'tesoro', 'x@y.test', 'X')",
            (HH,),
        )
