"""Phase 6: multi-user auth — tokens, the user store, and the REST endpoints.

Auth is stdlib-only (HMAC tokens + PBKDF2), so everything runs in CI. Auth is
always required; the endpoint tests below carry real bearer tokens (the
``real_auth`` marker opts out of conftest's auto-authenticated admin override) and
reset the user-store singleton around each so seeding is clean.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.auth import (
    Principal,
    assert_secure_auth_config,
    decode_token,
    hash_password,
    issue_token,
    reset_user_store,
    verify_password,
)
from api.auth.tokens import AuthError
from api.auth.users import DuplicateUser, InMemoryUserStore
from api.config import DEFAULT_AUTH_SECRET, settings
from api.main import app


@pytest.fixture(autouse=True)
def _reset_users() -> None:
    reset_user_store()
    yield
    reset_user_store()


# --- secure-config boot guard ------------------------------------------------


def test_assert_secure_auth_config_blocks_default_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_secret", DEFAULT_AUTH_SECRET)
    with pytest.raises(RuntimeError, match="API_AUTH_SECRET"):
        assert_secure_auth_config()


def test_assert_secure_auth_config_allows_overridden_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_secret", "a-real-strong-secret")
    assert_secure_auth_config()  # does not raise


# --- password hashing --------------------------------------------------------


def test_password_hash_roundtrip_and_uniqueness() -> None:
    a = hash_password("hunter2")
    b = hash_password("hunter2")
    assert a != b  # random per-hash salt
    assert verify_password("hunter2", a)
    assert not verify_password("wrong", a)
    assert not verify_password("hunter2", "garbage$not$a$hash")
    assert not verify_password("hunter2", "scrypt$1$aa$bb")  # unknown scheme
    # OWASP iteration count is applied (M2); the count is recorded in the hash so
    # older hashes with a different count still verify.
    assert a.split("$")[1] == "600000"
    legacy = hash_password("hunter2", iterations=120_000)
    assert verify_password("hunter2", legacy)


# --- tokens ------------------------------------------------------------------


def test_token_roundtrip_carries_principal() -> None:
    p = Principal(user_id="u1", household="acme", role="admin")
    token = issue_token(p, secret="s3cret", ttl_seconds=60, now=1000)
    got = decode_token(token, secret="s3cret", now=1010)
    assert got == p


def test_token_rejects_tamper_expiry_and_malformed() -> None:
    token = issue_token(Principal("u", "h"), secret="s3cret", ttl_seconds=60, now=1000)
    with pytest.raises(AuthError, match="expired"):
        decode_token(token, secret="s3cret", now=2000)
    with pytest.raises(AuthError, match="signature"):
        decode_token(token, secret="different", now=1010)
    with pytest.raises(AuthError, match="malformed"):
        decode_token("not-a-token", secret="s3cret", now=1010)
    # A correctly-signed payload that isn't valid JSON is still rejected.
    from api.auth.tokens import _b64url_encode, _sign

    bad_payload = _b64url_encode(b"not json")
    bad_token = f"{bad_payload}.{_sign(bad_payload, 's3cret')}"
    with pytest.raises(AuthError, match="malformed token payload"):
        decode_token(bad_token, secret="s3cret", now=1010)


def test_token_carries_username() -> None:
    p = Principal(user_id="u1", username="maya", household="acme", role="admin")
    token = issue_token(p, secret="s3cret", ttl_seconds=60, now=1000)
    got = decode_token(token, secret="s3cret", now=1010)
    assert got == p and got.username == "maya"


def test_old_token_without_username_decodes_empty() -> None:
    # A token minted before the "un" claim still decodes (username defaults to "").
    import json as _json

    from api.auth.tokens import _b64url_encode, _sign

    claims = {"sub": "u1", "hh": "acme", "role": "member", "iat": 1000, "exp": 9999999999}
    payload = _b64url_encode(_json.dumps(claims).encode())
    token = f"{payload}.{_sign(payload, 's3cret')}"
    got = decode_token(token, secret="s3cret", now=1010)
    assert got.username == "" and got.user_id == "u1"


# --- user store --------------------------------------------------------------


def test_user_store_create_authenticate_and_duplicates() -> None:
    store = InMemoryUserStore()
    user = store.create("Maya", "longpassword", household="acme", role="admin")
    assert store.authenticate("maya", "longpassword") is user  # case-insensitive
    assert store.authenticate("maya", "nope") is None
    assert store.authenticate("ghost", "x") is None
    with pytest.raises(DuplicateUser):
        store.create("maya", "another", household="acme")


def test_user_store_get_by_id() -> None:
    store = InMemoryUserStore()
    user = store.create("maya", "longpassword", household="acme")
    assert store.get_by_id(user.user_id) is user
    assert store.get_by_id("no-such-id") is None


def test_update_credentials_renames_and_keeps_id() -> None:
    store = InMemoryUserStore()
    user = store.create("maya", "longpassword", household="acme", role="admin")
    updated = store.update_credentials(user.user_id, username="maia")
    assert updated.user_id == user.user_id          # identity is stable
    assert updated.role == "admin"                  # untouched fields preserved
    assert store.get_by_username("maya") is None     # old username freed
    assert store.authenticate("maia", "longpassword") is updated
    assert store.get_by_id(user.user_id) is updated


def test_update_credentials_changes_password() -> None:
    store = InMemoryUserStore()
    user = store.create("maya", "oldpassword", household="acme")
    store.update_credentials(user.user_id, password="newpassword")
    assert store.authenticate("maya", "oldpassword") is None
    assert store.authenticate("maya", "newpassword") is not None


def test_update_credentials_rejects_collision_and_unknown_id() -> None:
    store = InMemoryUserStore()
    a = store.create("alice", "longpassword", household="acme")
    store.create("bob", "longpassword", household="acme")
    with pytest.raises(DuplicateUser):
        store.update_credentials(a.user_id, username="bob")  # taken by another user
    with pytest.raises(KeyError):
        store.update_credentials("ghost-id", username="x")


def test_list_by_household_scopes_and_sorts() -> None:
    store = InMemoryUserStore()
    store.create("zara", "longpassword", household="acme")
    store.create("Alice", "longpassword", household="acme", role="admin")
    store.create("carol", "longpassword", household="other")
    roster = store.list_by_household("acme")
    # Only the household's members, sorted case-insensitively by username.
    assert [u.username for u in roster] == ["Alice", "zara"]


def test_delete_removes_user_and_frees_username() -> None:
    store = InMemoryUserStore()
    user = store.create("maya", "longpassword", household="acme")
    store.delete(user.user_id)
    assert store.get_by_id(user.user_id) is None
    assert store.get_by_username("maya") is None
    # The username is free to reuse after deletion.
    again = store.create("maya", "longpassword", household="acme")
    assert again.user_id != user.user_id
    with pytest.raises(KeyError):
        store.delete("ghost-id")


def test_delete_clears_env_admin_marker() -> None:
    store = InMemoryUserStore()
    admin = store.create("root", "longpassword", household="acme", role="admin", is_env_admin=True)
    store.delete(admin.user_id)
    assert store.get_env_admin() is None


def test_create_marks_and_finds_the_env_admin() -> None:
    store = InMemoryUserStore()
    assert store.get_env_admin() is None
    store.create("alice", "longpassword", household="acme")           # not the env admin
    assert store.get_env_admin() is None
    admin = store.create("root", "longpassword", household="acme", role="admin", is_env_admin=True)
    assert store.get_env_admin() is admin


def _set_admin_env(monkeypatch: pytest.MonkeyPatch, user: str, pw: str, hh: str = "h1") -> None:
    monkeypatch.setattr(settings, "auth_admin_username", user)
    monkeypatch.setattr(settings, "auth_admin_password", pw)
    monkeypatch.setattr(settings, "auth_admin_household", hh)


def test_reconcile_creates_env_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth import get_user_store

    _set_admin_env(monkeypatch, "root", "rootpassword")
    reset_user_store()
    store = get_user_store()
    admin = store.authenticate("root", "rootpassword")
    assert admin is not None and admin.role == "admin" and admin.household == "h1"
    assert store.get_env_admin() is admin


def test_reconcile_updates_username_and_password_keeping_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth.users import InMemoryUserStore, reconcile_admin

    store = InMemoryUserStore()
    _set_admin_env(monkeypatch, "root", "rootpassword")
    reconcile_admin(store)
    original_id = store.get_env_admin().user_id

    # Operator edits both the username and the password in the env, then reboots.
    _set_admin_env(monkeypatch, "newroot", "newpassword")
    reconcile_admin(store)

    admin = store.get_env_admin()
    assert admin.user_id == original_id           # identity preserved
    assert admin.username == "newroot"
    assert store.authenticate("newroot", "newpassword") is admin
    assert store.authenticate("root", "rootpassword") is None


def test_reconcile_skips_on_username_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth.users import InMemoryUserStore, reconcile_admin

    store = InMemoryUserStore()
    _set_admin_env(monkeypatch, "root", "rootpassword")
    reconcile_admin(store)
    store.create("taken", "longpassword", household="h1")  # a different user owns "taken"

    _set_admin_env(monkeypatch, "taken", "rootpassword")  # collide the admin's new name
    reconcile_admin(store)  # logs + keeps the current admin username; must not raise

    admin = store.get_env_admin()
    assert admin.username == "root"  # unchanged
    assert store.authenticate("root", "rootpassword") is admin


def test_reconcile_noop_when_admin_vars_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth.users import InMemoryUserStore, reconcile_admin

    # Admin vars unset: nothing seeded (unsetting them is a no-op every boot).
    store = InMemoryUserStore()
    monkeypatch.setattr(settings, "auth_admin_username", "")
    monkeypatch.setattr(settings, "auth_admin_password", "")
    reconcile_admin(store)
    assert store.get_env_admin() is None


def test_get_user_store_selects_sql_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth import get_user_store
    from api.auth.sql_users import SqlUserStore

    monkeypatch.setattr(settings, "persistence_backend", "postgres")
    # Admin vars unset, so reconcile is a no-op and never touches the (absent) DB.
    monkeypatch.setattr(settings, "auth_admin_username", "")
    monkeypatch.setattr(settings, "auth_admin_password", "")
    reset_user_store()
    assert isinstance(get_user_store(), SqlUserStore)
    reset_user_store()


# --- endpoints ---------------------------------------------------------------


def _enable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_secret", "test-secret")


@pytest.mark.real_auth
def test_login_me_and_household_scoping(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth import get_user_store

    _enable_auth(monkeypatch)
    get_user_store().create("maya", "longpassword", household="acme", role="admin")

    with TestClient(app) as client:
        # No token -> 401 on a protected route; an invalid token is also 401.
        assert client.get("/conversations").status_code == 401
        assert client.get("/auth/me").status_code == 401
        assert (
            client.get("/conversations", headers={"Authorization": "Bearer nope"}).status_code
            == 401
        )

        bad = client.post("/auth/login", json={"username": "maya", "password": "wrong"})
        assert bad.status_code == 401

        tok = client.post("/auth/login", json={"username": "maya", "password": "longpassword"})
        assert tok.status_code == 200
        token = tok.json()["token"]
        assert tok.json()["household"] == "acme"

        assert tok.json()["username"] == "maya"

        auth = {"Authorization": f"Bearer {token}"}
        me = client.get("/auth/me", headers=auth).json()
        assert me["household"] == "acme"
        assert me["username"] == "maya"

        # The token's household scopes data, not a guessable query param.
        from api.persistence import get_conversation_store

        get_conversation_store().create("acme", "conv-acme")
        listed = client.get("/conversations", headers=auth).json()
        assert [c["id"] for c in listed] == ["conv-acme"]
        # A different household (different token) sees nothing.
        other = issue_token(
            Principal("u2", "other", "member"), secret="test-secret", ttl_seconds=60
        )
        assert (
            client.get("/conversations", headers={"Authorization": f"Bearer {other}"}).json()
            == []
        )


@pytest.mark.real_auth
def test_admin_can_create_user_member_cannot(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth import get_user_store

    _enable_auth(monkeypatch)
    get_user_store().create("admin", "longpassword", household="acme", role="admin")

    with TestClient(app) as client:
        token = client.post(
            "/auth/login", json={"username": "admin", "password": "longpassword"}
        ).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        r = client.post(
            "/auth/users",
            json={"username": "bob", "password": "longpassword", "role": "member"},
            headers=auth,
        )
        assert r.status_code == 201 and r.json()["household"] == "acme"
        # Duplicate username is rejected.
        assert (
            client.post(
                "/auth/users",
                json={"username": "bob", "password": "longpassword"},
                headers=auth,
            ).status_code
            == 409
        )

        # A member token cannot create users.
        member = client.post(
            "/auth/login", json={"username": "bob", "password": "longpassword"}
        ).json()["token"]
        forbidden = client.post(
            "/auth/users",
            json={"username": "eve", "password": "longpassword"},
            headers={"Authorization": f"Bearer {member}"},
        )
        assert forbidden.status_code == 403


@pytest.mark.real_auth
def test_admin_lists_and_removes_users(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth import get_user_store

    _enable_auth(monkeypatch)
    store = get_user_store()
    admin = store.create("admin", "longpassword", household="acme", role="admin")
    bob = store.create("bob", "longpassword", household="acme")
    # A member of another household must never appear in acme's roster.
    store.create("eve", "longpassword", household="other")

    with TestClient(app) as client:
        token = client.post(
            "/auth/login", json={"username": "admin", "password": "longpassword"}
        ).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        roster = client.get("/auth/users", headers=auth)
        assert roster.status_code == 200
        names = [u["username"] for u in roster.json()]
        assert names == ["admin", "bob"]  # household-scoped, sorted; no "eve"

        # Removing a member 204s and drops them from the roster.
        assert client.delete(f"/auth/users/{bob.user_id}", headers=auth).status_code == 204
        assert [u["username"] for u in client.get("/auth/users", headers=auth).json()] == ["admin"]

        # An admin can't delete their own account.
        assert client.delete(f"/auth/users/{admin.user_id}", headers=auth).status_code == 400
        # A user in another household is reported as not found (no cross-tenant leak).
        eve = store.get_by_username("eve")
        assert client.delete(f"/auth/users/{eve.user_id}", headers=auth).status_code == 404
        # An unknown id is 404 too.
        assert client.delete("/auth/users/ghost", headers=auth).status_code == 404


@pytest.mark.real_auth
def test_member_cannot_list_or_delete_users(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth import get_user_store

    _enable_auth(monkeypatch)
    store = get_user_store()
    store.create("admin", "longpassword", household="acme", role="admin")
    bob = store.create("bob", "longpassword", household="acme")

    with TestClient(app) as client:
        member = client.post(
            "/auth/login", json={"username": "bob", "password": "longpassword"}
        ).json()["token"]
        auth = {"Authorization": f"Bearer {member}"}
        assert client.get("/auth/users", headers=auth).status_code == 403
        assert client.delete(f"/auth/users/{bob.user_id}", headers=auth).status_code == 403


@pytest.mark.real_auth
def test_env_admin_cannot_be_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.auth import get_user_store

    _enable_auth(monkeypatch)
    store = get_user_store()
    store.create("admin", "longpassword", household="acme", role="admin")
    root = store.create(
        "root", "longpassword", household="acme", role="admin", is_env_admin=True
    )

    with TestClient(app) as client:
        token = client.post(
            "/auth/login", json={"username": "admin", "password": "longpassword"}
        ).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        # The roster marks the env admin so the UI can grey out its remove control.
        roster = {u["username"]: u for u in client.get("/auth/users", headers=auth).json()}
        assert roster["root"]["isEnvAdmin"] is True
        assert roster["admin"]["isEnvAdmin"] is False
        # And the server refuses to remove it (it would just reappear on reboot).
        assert client.delete(f"/auth/users/{root.user_id}", headers=auth).status_code == 409


@pytest.mark.real_auth
def test_ws_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.websockets import WebSocketDisconnect

    _enable_auth(monkeypatch)
    token = issue_token(
        Principal("u", "acme", "member"), secret="test-secret", ttl_seconds=60
    )
    with TestClient(app) as client:
        # No token -> the socket is closed (1008) before it can be used.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
        # A valid token in the query param connects and the session is scoped to it.
        with client.websocket_connect(f"/ws?token={token}") as ws:
            import json as _json

            ws.send_text(_json.dumps({"type": "session.start", "micSource": "phone-microphone"}))
            assert ws.receive_json()["type"] == "session.ready"




# --- SqlUserStore: psycopg row_factory wiring (regression) -------------------
#
# The SQL methods need a live Postgres, so they are normally exercised only by the
# compose stack (hence ``# pragma: no cover``) and psycopg isn't installed in CI.
# This pins the psycopg3 contract these methods must honour on two fronts:
#   1. ``Connection.execute()`` takes (query, params) — passing ``row_factory`` to
#      it raises TypeError (the original 500 on /auth/login).
#   2. ``row_factory`` must be scoped to a *cursor* (``conn.cursor(row_factory=…)``),
#      NOT assigned to the pooled *connection*. psycopg's pool doesn't reset
#      row_factory between checkouts, so ``conn.row_factory = dict_row`` poisons the
#      next borrower — the "Postgres connection going in and out" status flap.
# We stub ``psycopg`` in sys.modules (so the lazy imports resolve to a sentinel
# ``dict_row``); the fake connection's ``execute`` rejects a ``row_factory`` kwarg,
# its ``cursor`` accepts one, and its own ``row_factory`` must stay untouched.


@pytest.fixture
def fake_psycopg(monkeypatch: pytest.MonkeyPatch):
    import sys
    import types

    dict_row = object()  # opaque sentinel; we assert identity, not behavior

    class UniqueViolation(Exception):
        pass

    rows_mod = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = dict_row
    errors_mod = types.ModuleType("psycopg.errors")
    errors_mod.UniqueViolation = UniqueViolation
    pkg = types.ModuleType("psycopg")
    pkg.rows = rows_mod
    pkg.errors = errors_mod
    monkeypatch.setitem(sys.modules, "psycopg", pkg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_mod)
    monkeypatch.setitem(sys.modules, "psycopg.errors", errors_mod)
    return types.SimpleNamespace(dict_row=dict_row, UniqueViolation=UniqueViolation)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn", row_factory: object) -> None:
        self._conn = conn
        self.row_factory = row_factory
        self._row: object = None

    def execute(self, query: str, params: object = None) -> "_FakeCursor":
        # Record the row_factory in effect for this read so tests can assert it was
        # scoped here (cursor) rather than assigned to the pooled connection.
        self._conn.calls.append((query, params, self.row_factory))
        self._row = self._conn._next_row()
        return self

    def fetchone(self) -> object:
        return self._row


class _FakeConn:
    def __init__(self, rows: list[object]) -> None:
        # psycopg connections default to tuple rows; the store must opt into dict_row
        # per-cursor and leave this connection-level default untouched.
        self.row_factory = None
        self._rows = rows
        self._n = 0
        self.calls: list[tuple[str, object, object]] = []

    def _next_row(self) -> object:
        row = self._rows[self._n] if self._n < len(self._rows) else None
        self._n += 1
        return row

    def cursor(self, *, row_factory: object = None) -> _FakeCursor:
        # psycopg3's Connection.cursor() DOES accept row_factory — this is the
        # pool-safe way to get dict rows.
        return _FakeCursor(self, row_factory)

    def execute(self, query: str, params: object = None) -> _FakeCursor:
        # Deliberately no row_factory kwarg — mirrors psycopg3, so a call like
        # `conn.execute(..., row_factory=dict_row)` raises TypeError here.
        return _FakeCursor(self, self.row_factory).execute(query, params)

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connection(self) -> _FakeConn:
        return self._conn


def _store_with(conn: _FakeConn):
    from api.auth.sql_users import SqlUserStore

    store = SqlUserStore("postgresql://unused")
    store._pool = _FakePool(conn)  # bypass _ensure_pool (no real DB)
    return store


# Synthetic fixture credentials — never a real account. Keep them obviously fake so
# nothing in this repo can double as a working login.
_ADMIN_PASSWORD = "fixture-admin-password"

_ADMIN_ROW = {
    "id": "11111111-1111-1111-1111-111111111111",
    "household": "default",
    "username": "ada",
    "role": "admin",
    "password_hash": hash_password(_ADMIN_PASSWORD),
}


def test_sql_user_store_get_by_username_scopes_dict_row_to_cursor(fake_psycopg) -> None:
    conn = _FakeConn([_ADMIN_ROW])
    user = _store_with(conn).get_by_username("ada")
    # The row mapped cleanly (so dict access worked), the store scoped dict_row to the
    # cursor (not execute()'s kwargs), and it never poisoned the pooled connection.
    assert user is not None and user.username == "ada" and user.role == "admin"
    assert conn.calls and conn.calls[0][2] is fake_psycopg.dict_row
    assert conn.row_factory is None


def test_sql_user_store_authenticate_round_trips_password(fake_psycopg) -> None:
    # The full /auth/login path: look the user up, then verify the hash.
    good = _store_with(_FakeConn([_ADMIN_ROW])).authenticate("ada", _ADMIN_PASSWORD)
    assert good is not None and good.username == "ada"
    bad = _store_with(_FakeConn([_ADMIN_ROW])).authenticate("ada", "wrong")
    assert bad is None


def test_sql_user_store_create_and_update_scope_dict_row_to_cursor(fake_psycopg) -> None:
    conn = _FakeConn([_ADMIN_ROW])
    created = _store_with(conn).create("ada", _ADMIN_PASSWORD, household="default", role="admin")
    assert created.username == "ada"
    assert conn.calls[0][2] is fake_psycopg.dict_row
    assert conn.row_factory is None

    conn2 = _FakeConn([_ADMIN_ROW])
    updated = _store_with(conn2).update_credentials(_ADMIN_ROW["id"], password="newpass123")
    assert updated.username == "ada"
    assert conn2.calls[0][2] is fake_psycopg.dict_row
    assert conn2.row_factory is None


def test_sql_user_store_get_env_admin_and_by_id_scope_dict_row_to_cursor(fake_psycopg) -> None:
    conn = _FakeConn([_ADMIN_ROW])
    assert _store_with(conn).get_env_admin().username == "ada"
    assert conn.calls[0][2] is fake_psycopg.dict_row
    assert conn.row_factory is None

    conn2 = _FakeConn([_ADMIN_ROW])
    assert _store_with(conn2).get_by_id(_ADMIN_ROW["id"]).username == "ada"
    assert conn2.calls[0][2] is fake_psycopg.dict_row
    assert conn2.row_factory is None
