"""SqlUserStore username uniqueness against a REAL Postgres (XERK-1535).

``users.username`` was only case-sensitively UNIQUE while every lookup matches
``lower(username)``, so "alice" and "ALICE" could both be created and login resolved
to whichever row Postgres returned first. InMemoryUserStore rejects case variants, so
only a real database shows the gap. Skipped unless ``TENIR_TEST_PG_DSN`` is set (see
test_pg_schema_live.py); each test works in its own dropped-after schema.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest

from api.auth.users import DuplicateUser

DSN = os.environ.get("TENIR_TEST_PG_DSN", "")
psycopg = pytest.importorskip("psycopg") if DSN else None

pytestmark = pytest.mark.skipif(not DSN, reason="TENIR_TEST_PG_DSN not set (needs a real Postgres)")


@pytest.fixture
def make_store():
    """Build SqlUserStores on one fresh schema (each a separate "boot"); yields the
    factory and an autocommit admin connection on that schema."""
    from psycopg.conninfo import make_conninfo

    from api.auth.sql_users import SqlUserStore

    schema = f"t_{uuid.uuid4().hex[:12]}"
    dsn = make_conninfo(DSN, options=f"-c search_path={schema},public")
    stores: list[SqlUserStore] = []

    def make() -> SqlUserStore:
        store = SqlUserStore(dsn)
        store._ensure_pool()
        stores.append(store)
        return store

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        admin.execute(f"SET search_path TO {schema}, public")
        try:
            yield make, admin
        finally:
            for store in stores:
                store._pool.close()
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _has_lower_index(admin) -> bool:
    return (
        admin.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema()"
            " AND indexname = 'users_username_lower_idx'"
        ).fetchone()
        is not None
    )


def test_case_variant_usernames_are_duplicates(make_store) -> None:
    make, admin = make_store
    store = make()
    assert _has_lower_index(admin)
    alice = store.create("alice", "alice-pw-1", household="default")
    with pytest.raises(DuplicateUser):
        store.create("ALICE", "mallory-pw", household="default")
    with pytest.raises(DuplicateUser):
        store.create_oidc(
            oidc_sub="sub-1", email=None, username="Alice", household="default", role="member"
        )
    bob = store.create("bob", "bob-pw-1", household="default")
    with pytest.raises(DuplicateUser):
        store.update_credentials(bob.user_id, username="ALICE")
    with pytest.raises(DuplicateUser):
        store.update_oidc(bob.user_id, username="aLiCe")

    assert store.authenticate("ALICE", "alice-pw-1").user_id == alice.user_id
    assert store.authenticate("ALICE", "mallory-pw") is None


def test_legacy_case_variant_duplicates_do_not_abort_boot(make_store, caplog) -> None:
    make, admin = make_store
    make()
    # A database from before the index, already holding a case-variant pair.
    admin.execute("DROP INDEX users_username_lower_idx")
    admin.execute(
        "INSERT INTO users (household, username, password_hash, created_at)"
        " VALUES ('default', 'alice', 'x', now() - interval '1 day'),"
        "        ('default', 'ALICE', 'y', now())"
    )

    with caplog.at_level(logging.ERROR, logger="api.auth.sql_users"):
        store = make()  # boot on the upgraded image
    assert not _has_lower_index(admin)
    assert "['alice', 'ALICE']" in caplog.text

    # Deterministic: the exact-case row, else the oldest.
    assert store.get_by_username("ALICE").username == "ALICE"
    assert store.get_by_username("alice").username == "alice"
    assert store.get_by_username("Alice").username == "alice"

    # Once the operator removes the duplicate, the next boot enforces uniqueness.
    store.delete(store.get_by_username("ALICE").user_id)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="api.auth.sql_users"):
        store = make()
    assert _has_lower_index(admin)
    assert caplog.text == ""
    with pytest.raises(DuplicateUser):
        store.create("ALICE", "mallory-pw", household="default")


# --- Users in a non-default household (XERK-1508) -----------------------------
#
# users.household REFERENCES households(id) and schema.sql seeds only 'default', so
# creating the env admin with API_AUTH_ADMIN_HOUSEHOLD=home (or any member/OIDC user
# created or moved into a new household) failed users_household_fkey.


@pytest.fixture
def db(make_store):
    make, admin = make_store
    return make(), admin


def _households(admin) -> set[str]:
    return {r[0] for r in admin.execute("SELECT id FROM households").fetchall()}


def test_env_admin_seeds_into_a_non_default_household(db) -> None:
    store, admin = db
    user = store.create("admin", "pw-123456", household="home", role="admin", is_env_admin=True)
    assert user.household == "home"
    assert store.get_env_admin().user_id == user.user_id
    assert store.authenticate("admin", "pw-123456") is not None
    assert "home" in _households(admin)
    # A second user in the same household reuses the row rather than conflicting.
    assert store.create("kid", "pw-123456", household="home").household == "home"


def test_oidc_user_is_created_into_a_new_household(db) -> None:
    store, admin = db
    user = store.create_oidc(
        oidc_sub="sub-1", email="a@example.com", username="a", household="away", role="member"
    )
    assert user.household == "away"
    assert "away" in _households(admin)


def test_user_moves_into_a_new_household(db) -> None:
    store, admin = db
    user = store.create("m", "pw-123456", household="default")
    moved = store.update_credentials(user.user_id, household="elsewhere")
    assert moved.household == "elsewhere"
    assert "elsewhere" in _households(admin)


def test_a_rejected_create_leaves_no_household_behind(db) -> None:
    store, admin = db
    store.create("dup", "pw-123456", household="default")
    with pytest.raises(DuplicateUser):
        store.create("dup", "pw-123456", household="orphan")
    assert "orphan" not in _households(admin)


def test_moving_a_missing_user_leaves_no_household_behind(db) -> None:
    store, admin = db
    with pytest.raises(KeyError):
        store.update_credentials(str(uuid.uuid4()), household="ghost")
    assert "ghost" not in _households(admin)
