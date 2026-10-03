"""SqlUserStore writes into a non-default household against a REAL Postgres (XERK-1508).

``users.household REFERENCES households(id)`` and schema.sql seeds only 'default', so
creating the env admin with ``API_AUTH_ADMIN_HOUSEHOLD=home`` (or any member/OIDC user
created or moved into a new household) failed ``users_household_fkey``. The in-memory
store has no such constraint, so only a real database shows it.

Skipped unless ``TENIR_TEST_PG_DSN`` points at a disposable Postgres (CI provides one,
see test_pg_schema_live.py). Each test works in its own throwaway schema.
"""

from __future__ import annotations

import os
import uuid

import pytest

from api.auth.users import DuplicateUser
from api.persistence.postgres import apply_schema, find_schema_file

DSN = os.environ.get("TENIR_TEST_PG_DSN", "")
psycopg = pytest.importorskip("psycopg") if DSN else None

pytestmark = pytest.mark.skipif(not DSN, reason="TENIR_TEST_PG_DSN not set (needs a real Postgres)")


@pytest.fixture
def db():
    """(store, admin conn) over a fresh schema with schema.sql applied, dropped after."""
    from psycopg.conninfo import make_conninfo

    from api.auth.sql_users import SqlUserStore

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        admin.execute(f"SET search_path TO {schema}, public")
        apply_schema(admin, find_schema_file().read_text(encoding="utf-8"))
        s = SqlUserStore(make_conninfo(DSN, options=f"-c search_path={schema},public"))
        try:
            yield s, admin
        finally:
            if s._pool is not None:
                s._pool.close()
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


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
