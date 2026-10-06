"""SqlUserStore username uniqueness against a REAL Postgres (XERK-1535).

``users.username`` was only case-sensitively UNIQUE while every lookup matches
``lower(username)``, so "alice" and "ALICE" could both be created and login resolved
to whichever row Postgres returned first. InMemoryUserStore rejects case variants, so
only a real database shows the gap. Skipped unless ``TENIR_TEST_PG_DSN`` is set (see
test_pg_schema_live.py); each test works in its own dropped-after schema.

``users.id`` is a UUID column, so ``WHERE id = 'x'`` raised InvalidTextRepresentation
(22P02) instead of matching nothing: PATCH/DELETE /auth/users/x 500'd here where the
in-memory store 404s (XERK-1532). A non-canonical id is now "no such user".

Writes stored the username as given while lookups strip it, so " alice" was created
beside "alice" and could never log in (XERK-1548). Writes now store the stripped
name, and ``users_username_norm_idx`` (on the lower-cased, trimmed name) replaces the
case-only ``users_username_lower_idx``.
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


def _has_index(admin, name: str) -> bool:
    return (
        admin.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s",
            (name,),
        ).fetchone()
        is not None
    )


NORM_IDX = "users_username_norm_idx"
LOWER_IDX = "users_username_lower_idx"


def test_case_variant_usernames_are_duplicates(make_store) -> None:
    make, admin = make_store
    store = make()
    assert _has_index(admin, NORM_IDX)
    assert not _has_index(admin, LOWER_IDX)  # subsumed by the norm index
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
    admin.execute(f"DROP INDEX {NORM_IDX}")
    admin.execute(
        "INSERT INTO users (household, username, password_hash, created_at)"
        " VALUES ('default', 'alice', 'x', now() - interval '1 day'),"
        "        ('default', 'ALICE', 'y', now())"
    )

    with caplog.at_level(logging.ERROR, logger="api.auth.sql_users"):
        store = make()  # boot on the upgraded image
    assert not _has_index(admin, NORM_IDX)
    assert not _has_index(admin, LOWER_IDX)
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
    assert _has_index(admin, NORM_IDX)
    assert caplog.text == ""
    with pytest.raises(DuplicateUser):
        store.create("ALICE", "mallory-pw", household="default")


def test_whitespace_variant_usernames_are_duplicates(make_store) -> None:
    make, _ = make_store
    store = make()
    alice = store.create("alice", "alice-pw-1", household="default")
    for padded in (" alice", "alice ", "\tALICE\n"):
        with pytest.raises(DuplicateUser):
            store.create(padded, "mallory-pw", household="default")
    with pytest.raises(DuplicateUser):
        store.create_oidc(
            oidc_sub="sub-1", email=None, username=" alice", household="default", role="member"
        )

    # A padded name is stored stripped, so it logs in by the name it is listed under.
    bob = store.create(" bob ", "bob-pw-1", household="default")
    assert bob.username == "bob"
    assert store.authenticate("bob", "bob-pw-1").user_id == bob.user_id
    with pytest.raises(DuplicateUser):
        store.update_credentials(bob.user_id, username=" alice ")
    with pytest.raises(DuplicateUser):
        store.update_oidc(bob.user_id, username="alice\t")
    assert store.update_credentials(bob.user_id, username=" rob ").username == "rob"
    assert store.update_oidc(bob.user_id, username=" robin").username == "robin"
    kim = store.create_oidc(
        oidc_sub="sub-2", email=None, username=" kim ", household="default", role="member"
    )
    assert kim.username == "kim"
    assert store.get_by_username("kim").user_id == kim.user_id
    assert store.authenticate(" alice ", "alice-pw-1").user_id == alice.user_id


def test_legacy_padded_username_still_logs_in(make_store) -> None:
    """A row written padded before the fix matches its stripped name, and blocks a
    new user taking that name."""
    make, admin = make_store
    make()
    admin.execute(f"DROP INDEX {NORM_IDX}")
    admin.execute(
        "INSERT INTO users (household, username, password_hash)"
        " VALUES ('default', E' dave\\t', 'x'), ('default', E'\\x1ccarl\\x1f', 'y')"
    )
    store = make()
    assert _has_index(admin, NORM_IDX)
    assert store.get_by_username("Dave").username == " dave\t"
    # The key trims what str.strip() trims, not just btrim's default ' '.
    assert store.get_by_username("carl").username == "\x1ccarl\x1f"
    for name in ("dave", "carl"):
        with pytest.raises(DuplicateUser):
            store.create(name, "mallory-pw", household="default")


def test_blank_login_never_matches_a_legacy_blank_username(make_store) -> None:
    """A name str.strip() empties (stored raw before XERK-1548) has a blank key; a
    blank login must not resolve to it."""
    make, admin = make_store
    make()
    admin.execute(
        "INSERT INTO users (household, username, password_hash) VALUES ('default', E'\\x1f', 'x')"
    )
    store = make()
    for blank in ("", " ", "\t", "\x1f"):
        assert store.get_by_username(blank) is None


def test_legacy_whitespace_duplicates_keep_case_uniqueness(make_store, caplog) -> None:
    """Whitespace-only duplicates block the norm index but not the case-only one,
    which stays as the fallback so case variants are still refused."""
    make, admin = make_store
    make()
    admin.execute(f"DROP INDEX {NORM_IDX}")
    admin.execute(
        "INSERT INTO users (household, username, password_hash, created_at)"
        " VALUES ('default', ' eve', 'x', now() - interval '1 day'),"
        "        ('default', 'eve', 'y', now())"
    )
    with caplog.at_level(logging.ERROR, logger="api.auth.sql_users"):
        store = make()
    assert not _has_index(admin, NORM_IDX)
    assert _has_index(admin, LOWER_IDX)
    assert "[' eve', 'eve']" in caplog.text
    # Login resolves to the exact row, not the older padded one.
    assert store.get_by_username(" eve ").username == "eve"
    with pytest.raises(DuplicateUser):
        store.create("EVE", "mallory-pw", household="default")


def test_upgrade_replaces_the_case_only_index(make_store) -> None:
    """A database from XERK-1535 has only users_username_lower_idx; the next boot
    builds the norm index and drops the one it subsumes."""
    make, admin = make_store
    make()
    admin.execute(f"DROP INDEX {NORM_IDX}")
    admin.execute(f"CREATE UNIQUE INDEX {LOWER_IDX} ON users (lower(username))")
    make()
    assert _has_index(admin, NORM_IDX)
    assert not _has_index(admin, LOWER_IDX)


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


def test_create_whitespace_variant_of_an_existing_user_is_409(client, store) -> None:
    store.create("alice", "longpassword", household=settings.household_id)
    r = client.post("/auth/users", json={"username": " alice", "password": "longpassword"})
    assert r.status_code == 409, r.text


def test_patch_and_delete_a_real_user_still_work(client, store) -> None:
    user = store.create("maya", "longpassword", household=settings.household_id)
    patched = client.patch(f"/auth/users/{user.user_id}", json={"role": "admin"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["role"] == "admin"
    assert client.delete(f"/auth/users/{user.user_id}").status_code == 204
    assert store.get_by_id(user.user_id) is None
