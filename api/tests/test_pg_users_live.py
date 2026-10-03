"""SqlUserStore username uniqueness against a REAL Postgres (XERK-1535).

``users.username`` was only case-sensitively UNIQUE while every lookup matches
``lower(username)``, so "alice" and "ALICE" could both be created and login resolved
to whichever row Postgres returned first. InMemoryUserStore rejects case variants, so
only a real database shows the gap. Skipped unless ``TENIR_TEST_PG_DSN`` is set (see
test_pg_schema_live.py); each test works in its own dropped-after schema.

``users.id`` is a UUID column, so ``WHERE id = 'x'`` raised InvalidTextRepresentation
(22P02) instead of matching nothing: PATCH/DELETE /auth/users/x 500'd here where the
in-memory store 404s (XERK-1532). A non-canonical id is now "no such user".
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from api.auth.users import DuplicateUser
from api.config import settings
from api.main import app

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


BAD_IDS = ["x", "not-a-uuid", "1", "' OR 1=1 --", f"urn:uuid:{uuid.uuid4()}"]


@pytest.fixture
def store(make_store):
    make, _ = make_store
    return make()


@pytest.fixture
def client(store, monkeypatch: pytest.MonkeyPatch):
    """The app with the SQL store as the process-wide user store (conftest's admin
    override still authenticates every request as an admin)."""
    import api.auth.users as users

    monkeypatch.setattr(users, "_store", store)
    monkeypatch.setattr(users, "_admin_reconciled", True)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_store_lookups_treat_a_malformed_id_as_missing(store, bad: str) -> None:
    store.create("maya", "longpassword", household="default")  # a non-empty table
    assert store.get_by_id(bad) is None
    with pytest.raises(KeyError):
        store.delete(bad)
    with pytest.raises(KeyError):
        store.update_credentials(bad, email="m@example.com")
    with pytest.raises(KeyError):
        store.update_credentials(bad)
    with pytest.raises(KeyError):
        store.update_oidc(bad, role="member")


@pytest.mark.parametrize("bad", BAD_IDS)
def test_patch_and_delete_a_malformed_user_id_is_404(client, bad: str) -> None:
    patched = client.patch(f"/auth/users/{bad}", json={"role": "member"})
    assert patched.status_code == 404, patched.text
    deleted = client.delete(f"/auth/users/{bad}")
    assert deleted.status_code == 404, deleted.text


def test_non_canonical_spellings_of_a_real_id_do_not_match(client, store) -> None:
    """Postgres would cast ``{id}``/uppercase/unhyphenated to the same UUID, but the
    router's self-delete and env-admin guards compare the raw string — so only the
    canonical form may match, as on InMemoryUserStore."""
    user = store.create("maya", "longpassword", household=settings.household_id)
    u = uuid.UUID(user.user_id)
    for spelling in (f"{{{u}}}", str(u).upper(), u.hex):
        assert store.get_by_id(spelling) is None
        assert client.delete(f"/auth/users/{spelling}").status_code == 404
    assert store.get_by_id(user.user_id) is not None


def test_patch_and_delete_a_real_user_still_work(client, store) -> None:
    user = store.create("maya", "longpassword", household=settings.household_id)
    patched = client.patch(f"/auth/users/{user.user_id}", json={"role": "admin"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["role"] == "admin"
    assert client.delete(f"/auth/users/{user.user_id}").status_code == 204
    assert store.get_by_id(user.user_id) is None
