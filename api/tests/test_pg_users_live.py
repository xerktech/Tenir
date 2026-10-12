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

from api.auth.tokens import hash_password
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


def _skip_unless_utf8(admin) -> None:
    """For tests whose legacy rows hold characters only a UTF8 database can store."""
    if admin.execute("SHOW server_encoding").fetchone()[0] != "UTF8":
        pytest.skip("needs a UTF8 database")


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
    """A row written padded (by an old replica mid-rollout) matches its stripped name,
    and blocks a new user taking that name; the next boot stores it stripped."""
    make, admin = make_store
    store = make()
    admin.execute(
        "INSERT INTO users (household, username, password_hash)"
        " VALUES ('default', E' dave\\t', 'x'), ('default', E'\\x1ccarl\\x1f', 'y')"
    )
    assert store.get_by_username("Dave").username == " dave\t"
    # The key trims what str.strip() trims, not just btrim's default ' '.
    assert store.get_by_username("carl").username == "\x1ccarl\x1f"
    for name in ("dave", "carl"):
        with pytest.raises(DuplicateUser):
            store.create(name, "mallory-pw", household="default")
    store = make()
    assert store.get_by_username("Dave").username == "dave"
    assert store.get_by_username("carl").username == "carl"


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


# --- Legacy states and non-UTF8 databases (XERK-1591) --------------------------


def test_fallback_mode_refuses_a_whitespace_variant_of_a_legacy_name(
    make_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While whitespace duplicates block the norm index only the case-only index exists,
    so a new "bob" beside a legacy " bob\\t" slipped past it, and login "bob" then
    resolved to the new row: a takeover of the legacy account."""
    import api.auth.users as users

    make, admin = make_store
    make()
    admin.execute(f"DROP INDEX {NORM_IDX}")
    admin.execute(
        "INSERT INTO users (household, username, password_hash) VALUES"
        " ('default', 'alice', 'x'), ('default', ' alice', 'x')"
    )
    store = make()
    assert not _has_index(admin, NORM_IDX) and _has_index(admin, LOWER_IDX)
    # Written after boot (which strips padding where it can), as by an old replica.
    admin.execute(
        "INSERT INTO users (household, username, password_hash) VALUES ('default', E' bob\\t', 'x')"
    )
    monkeypatch.setattr(users, "_store", store)
    monkeypatch.setattr(users, "_admin_reconciled", True)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/auth/users", json={"username": "bob", "password": "longpassword"})
    assert r.status_code == 409, r.text
    with pytest.raises(DuplicateUser):
        store.create("BOB", "longpassword", household="default")
    with pytest.raises(DuplicateUser):
        store.create_oidc(
            oidc_sub="s", email=None, username="bob", household="default", role="member"
        )
    carol = store.create("carol", "longpassword", household="default")
    with pytest.raises(DuplicateUser):
        store.update_credentials(carol.user_id, username="bob")
    with pytest.raises(DuplicateUser):
        store.update_oidc(carol.user_id, username="Bob")
    assert store.get_by_username("bob").username == " bob\t"
    # Renaming a row to its own name (any case or padding) is not a clash.
    assert store.update_credentials(carol.user_id, username=" Carol").username == "Carol"


def test_one_strip_conflict_does_not_roll_back_the_others(make_store, caplog) -> None:
    """The boot strip commits per row: a create racing it (which doesn't take the
    schema lock) fails only that row's UPDATE, not every strip in the boot."""
    make, admin = make_store
    make()
    admin.execute(
        "INSERT INTO users (household, username, password_hash, created_at) VALUES"
        " ('default', ' boom', 'x', now() - interval '4 s'),"
        " ('default', ' ok1', 'x', now() - interval '3 s'),"
        " ('default', ' bad', 'x', now() - interval '2 s'),"
        " ('default', ' ok2', 'x', now() - interval '1 s')"
    )
    # Stands in for the racing create: the UPDATE to "bad" hits a unique violation.
    # "boom" (stripped first) fails some other way; neither holds back later rows.
    admin.execute(
        """
        CREATE FUNCTION fail_bad() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.username = 'bad' THEN
                RAISE EXCEPTION 'raced' USING ERRCODE = 'unique_violation';
            END IF;
            IF NEW.username = 'boom' THEN
                RAISE EXCEPTION 'some other failure';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    admin.execute(
        "CREATE TRIGGER fail_bad BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION fail_bad()"
    )
    with caplog.at_level(logging.WARNING, logger="api.auth.sql_users"):
        store = make()
    assert store.get_by_username("ok1").username == "ok1"
    assert store.get_by_username("ok2").username == "ok2"
    assert store.get_by_username("bad").username == " bad"
    assert store.get_by_username("boom").username == " boom"
    assert "' bad'" in caplog.text and "some other failure" in caplog.text


def _ascii_json(body: dict) -> dict:
    """Request kwargs sending ``body`` as \\u-escaped JSON: httpx's own json= encoder
    can't encode a lone surrogate, which a real client can still send escaped."""
    import json

    return {"content": json.dumps(body), "headers": {"content-type": "application/json"}}


def test_nul_and_surrogate_names_are_clean_refusals(store, monkeypatch) -> None:
    """A NUL (Postgres text can't hold it) or lone surrogate (not UTF-8 encodable)
    raised out of psycopg: a 500 on login and on create. Now 401 / 422."""
    import api.auth.users as users
    from api.auth.users import InvalidUserValue

    monkeypatch.setattr(users, "_store", store)
    monkeypatch.setattr(users, "_admin_reconciled", True)
    client = TestClient(app, raise_server_exceptions=False)
    store.create("nora", "longpassword", household=settings.household_id)
    for bad in ("no\x00ra", "nora\ud800", "\udfff"):
        r = client.post("/auth/login", **_ascii_json({"username": bad, "password": "longpassword"}))
        assert r.status_code == 401, r.text
        r = client.post("/auth/users", **_ascii_json({"username": bad, "password": "longpassword"}))
        assert r.status_code == 422, r.text
        assert store.get_by_username(bad) is None
        assert store.get_by_email(bad + "@example.com") is None
        assert store.get_by_oidc_sub(bad) is None
        with pytest.raises(InvalidUserValue):
            store.create(bad, "longpassword", household="default")
    with pytest.raises(InvalidUserValue):
        store.create("nell", "longpassword", household="default", email="n\x00@example.com")
    for body in (
        {"username": "nell", "password": "longpassword", "email": "n\x00@example.com"},
        {"username": "nell", "password": "long\ud800password"},
    ):
        r = client.post("/auth/users", **_ascii_json(body))
        assert r.status_code == 422, r.text
    r = client.post("/auth/login", **_ascii_json({"username": "nora", "password": "long\ud800"}))
    assert r.status_code == 401, r.text


def test_norm_index_race_falls_back_instead_of_failing(make_store) -> None:
    """A rolling update: an old replica commits a whitespace duplicate after the DO
    block's duplicate check but before CREATE UNIQUE INDEX, which then failed 23505 and
    the apply raised SchemaApplyError. It now falls back to the case-only index."""
    import threading

    from api.auth.sql_users import _ENSURE_SCHEMA

    make, admin = make_store
    make()
    admin.execute(f"DROP INDEX {NORM_IDX}")
    admin.execute(
        "INSERT INTO users (household, username, password_hash) VALUES ('default', 'eve', 'x')"
    )
    search_path = admin.execute("SHOW search_path").fetchone()[0]
    with psycopg.connect(DSN) as writer, psycopg.connect(DSN, autocommit=True) as applier:
        for conn in (writer, applier):
            conn.execute(f"SET search_path TO {search_path}")
        writer.commit()
        # Uncommitted, so the DO block's check doesn't see it; its CREATE INDEX then
        # waits on the writer's lock and meets the row once it commits.
        writer.execute(
            "INSERT INTO users (household, username, password_hash)"
            " VALUES ('default', ' eve', 'y')"
        )
        errors: list[BaseException] = []

        def apply() -> None:
            try:
                applier.execute(_ENSURE_SCHEMA[-1])
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        thread = threading.Thread(target=apply)
        thread.start()
        pid = applier.info.backend_pid
        waiting = None
        for _ in range(200):
            waiting = admin.execute(
                "SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted", (pid,)
            ).fetchone()
            if waiting:
                break
            thread.join(0.05)
        assert waiting, "the DO block never blocked on the writer"
        writer.commit()
        thread.join(10)
    assert not thread.is_alive()
    assert errors == []
    assert not _has_index(admin, NORM_IDX)
    assert _has_index(admin, LOWER_IDX)


def test_legacy_non_ascii_padding_is_stripped_at_boot(make_store, caplog) -> None:
    """The username key trims only ASCII, so a legacy name padded with e.g. NBSP or
    U+3000 never matched its stripped name and could not log in. Boot strips such rows
    unless the stripped name is already taken (or blank); those are logged."""
    make, admin = make_store
    _skip_unless_utf8(admin)
    make()
    admin.execute(
        "INSERT INTO users (household, username, password_hash) VALUES"
        " ('default', %s, 'x'), ('default', %s, 'x'), ('default', %s, 'x'),"
        " ('default', %s, 'x'), ('default', 'quinn', 'x'), ('default', %s, 'x')",
        (" nina", "　omar ", "pat\x85", " Quinn ", " "),
    )
    with caplog.at_level(logging.WARNING, logger="api.auth.sql_users"):
        store = make()
    assert store.get_by_username("nina").username == "nina"
    assert store.get_by_username("OMAR").username == "omar"
    assert store.get_by_username("pat").username == "pat"
    # Stripping would collide with "quinn" or leave a blank name: left alone, logged.
    names = {r[0] for r in admin.execute("SELECT username FROM users").fetchall()}
    assert {" Quinn ", " ", "quinn"} <= names
    assert "Quinn" in caplog.text and "\\xa0" in caplog.text
    with pytest.raises(DuplicateUser):
        store.create("nina", "longpassword", household="default")


def test_padding_strip_waits_for_the_schema_lock(make_store) -> None:
    """Two replicas booting together could each strip a different row onto the same
    name, so the strip runs under the cross-process schema lock."""
    import threading

    from api.auth.sql_users import SqlUserStore
    from api.persistence.postgres import SCHEMA_LOCK_KEY

    make, admin = make_store
    store = make()
    admin.execute(
        "INSERT INTO users (household, username, password_hash) VALUES ('default', ' lou', 'x')"
    )
    with psycopg.connect(DSN, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_KEY,))
        thread = threading.Thread(
            target=SqlUserStore._strip_padded_usernames, args=(store._pool,)
        )
        thread.start()
        thread.join(1.0)
        assert thread.is_alive(), "the strip ran without the schema lock"
        assert store.get_by_username("lou").username == " lou"
        holder.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_KEY,))
        thread.join(10)
    assert not thread.is_alive()
    assert store.get_by_username("lou").username == "lou"


@pytest.fixture
def make_encoded_store():
    """factory(encoding) -> (SqlUserStore, admin connection) on a fresh database of that
    server encoding, dropped afterwards."""
    from psycopg.conninfo import make_conninfo

    from api.auth.sql_users import SqlUserStore

    created: list[tuple[str, SqlUserStore, object]] = []

    def make(encoding: str):
        name = f"t_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(DSN, autocommit=True) as root:
            root.execute(
                f"CREATE DATABASE {name} ENCODING '{encoding}'"
                " LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0"
            )
        dsn = make_conninfo(DSN, dbname=name)
        store = SqlUserStore(dsn)
        admin = psycopg.connect(dsn, autocommit=True, client_encoding="UTF8")
        created.append((name, store, admin))
        store._ensure_pool()
        return store, admin

    yield make
    with psycopg.connect(DSN, autocommit=True) as root:
        for name, store, admin in created:
            if store._pool is not None:
                store._pool.close()
            admin.close()
            root.execute(f"DROP DATABASE {name} WITH (FORCE)")


def test_sql_ascii_database_logs_in(make_encoded_store) -> None:
    """On a SQL_ASCII database psycopg returned every text column as bytes, so the
    password hash never verified and every login (the env admin's too) 401'd."""
    store, _ = make_encoded_store("SQL_ASCII")
    user = store.create(
        "admin", "longpassword", household="default", role="admin", email="a@b.c"
    )
    assert isinstance(user.username, str)
    assert store.authenticate("admin", "longpassword").user_id == user.user_id
    assert store.get_by_email("A@b.c").user_id == user.user_id
    jo = store.create("josé", "longpassword", household="default")
    assert store.get_by_username("josé").user_id == jo.user_id


def test_latin1_database_treats_unencodable_input_as_no_such_user(make_encoded_store) -> None:
    """A lookup holding a character LATIN1 can't store matches no row, rather than
    raising (a 500); a write of one is refused as an invalid value."""
    from api.auth.sql_users import SqlUserStore
    from api.auth.users import InvalidUserValue

    store, admin = make_encoded_store("LATIN1")
    store.create("josé", "longpassword", household="default", email="j@example.com")
    assert store.get_by_username("josé").username == "josé"
    assert store.get_by_email("　x@example.com") is None
    assert store.get_by_username("漢字") is None
    assert store.authenticate("漢字", "longpassword") is None
    assert store.get_by_oidc_sub("漢") is None
    with pytest.raises(InvalidUserValue):
        store.create("漢字", "longpassword", household="default")
    with pytest.raises(InvalidUserValue):
        store.create_oidc(
            oidc_sub="s1",
            email="　x@example.com",
            username="x",
            household="default",
            role="member",
        )
    # Fullwidth doesn't encode in LATIN1, but its NFKC form (what is stored) does.
    wide = store.create("ｃａｒｌ", "longpassword", household="default")
    assert wide.username == "carl"
    assert store.authenticate("ｃａｒｌ", "longpassword").user_id == wide.user_id
    jo = store.get_by_username("josé")
    with pytest.raises(InvalidUserValue):
        store.update_credentials(jo.user_id, email="　x@example.com")
    with pytest.raises(InvalidUserValue):
        store.update_oidc(jo.user_id, username="漢")
    # A legacy LATIN1 name padded with NBSP / NEL is stripped at boot, as on UTF8.
    admin.execute(
        "INSERT INTO users (household, username, password_hash) VALUES ('default', %s, 'x')",
        (" nina\x85",),
    )
    SqlUserStore._ensure_schema(store._pool)
    assert store.get_by_username("nina").username == "nina"
    # The mirror: a legacy name LATIN1 holds whose NFKC form it can't (micro sign ->
    # Greek mu, one-half -> 1/2 with U+2044) still logs in by its own spelling.
    admin.execute(
        "INSERT INTO users (household, username, password_hash) VALUES"
        " ('default', %s, %s), ('default', %s, %s)",
        ("µser", hash_password("longpassword"), "½x", hash_password("longpassword")),
    )
    for legacy in ("µser", "½x", " µser "):
        assert store.authenticate(legacy, "longpassword") is not None, legacy


def test_latin1_oidc_token_with_an_unencodable_email_is_401(
    make_encoded_store, oidc_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verified email is looked up (and would be stored) before JIT: a U+3000 in it
    raised UnicodeEncodeError out of get_by_email on LATIN1, a 500."""
    import api.auth.users as users
    from api.auth.deps import current_principal

    store, _ = make_encoded_store("LATIN1")
    monkeypatch.setattr(users, "_store", store)
    monkeypatch.setattr(users, "_admin_reconciled", True)
    monkeypatch.delitem(app.dependency_overrides, current_principal, raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    token = oidc_env.mint(
        sub="sub-u3000", email="　x@example.com", preferred_username="x"
    )
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text
    # A non-Latin-1 preferred_username alone falls through to the next candidate.
    token = oidc_env.mint(sub="sub-kanji", email="k@example.com", preferred_username="漢")
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "k"


def test_new_usernames_are_nfkc_and_refuse_format_chars(make_store, client) -> None:
    """Operator decision (XERK-1591): "carl" + a zero-width char could be created beside
    "carl". New names are NFKC-normalised and may not hold format (Cf) characters;
    legacy rows are left alone and still log in by their exact name."""
    from api.auth.users import InvalidUserValue

    make, admin = make_store
    _skip_unless_utf8(admin)
    store = make()
    admin.execute(
        "INSERT INTO users (household, username, password_hash) VALUES ('default', %s, 'x')",
        ("dave​",),
    )
    carl = store.create("carl", "longpassword", household=settings.household_id)
    for bad in ("carl​", "c‍arl", "⁠carl", "carl﻿"):
        with pytest.raises(InvalidUserValue):
            store.create(bad, "longpassword", household="default")
        with pytest.raises(InvalidUserValue):
            store.update_credentials(carl.user_id, username=bad)
        with pytest.raises(InvalidUserValue):
            store.update_oidc(carl.user_id, username=bad)
        with pytest.raises(InvalidUserValue):
            store.create_oidc(
                oidc_sub="s", email=None, username=bad, household="default", role="member"
            )
        r = client.post("/auth/users", json={"username": bad, "password": "longpassword"})
        assert r.status_code == 422, r.text
    wide = store.create("ｅｍｍａ", "longpassword", household="default")
    assert wide.username == "emma"
    for login in ("emma", "ｅｍｍａ", "EMMA"):
        assert store.authenticate(login, "longpassword").user_id == wide.user_id
    with pytest.raises(DuplicateUser):
        store.create("emma", "longpassword", household="default")
    assert store.get_by_username("dave​").username == "dave​"
