"""Per-device revocation of screen-capture device tokens (XERK-2594).

A device token is stateless HMAC carrying its device_tokens row id as ``jti``; /ws/screen
opens only while that row is live, so one capture PC can be cut off without logging out
every other device (which rotating ``API_AUTH_SECRET`` would).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.auth import AuthError, Principal, get_user_store, issue_token, principal_from_bearer
from api.config import settings
from api.metrics import metrics
from api.persistence import InMemoryConversationStore
from api.screen import ws as screen_ws

# These tests present real tokens: opt out of conftest's admin override.
pytestmark = pytest.mark.real_auth


class _NoOcr:
    """OCR is never reached: these tests only open and close sockets."""

    async def ocr(self, *_args, **_kwargs):  # pragma: no cover - not called
        raise AssertionError("unexpected OCR call")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    from api.auth import reset_user_store
    from api.main import app

    reset_user_store()
    for name in ("op", "other"):
        get_user_store().create(name, f"{name}-password-123", household="default")
    monkeypatch.setattr(screen_ws, "get_ocr_client", lambda: _NoOcr())
    monkeypatch.setattr(screen_ws, "get_conversation_store", InMemoryConversationStore)
    metrics.reset()
    with TestClient(app) as c:
        yield c
    reset_user_store()


def _login(client: TestClient, username: str) -> dict[str, str]:
    r = client.post(
        "/auth/login", json={"username": username, "password": f"{username}-password-123"}
    )
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _mint(client: TestClient, bearer: dict[str, str], name: str | None = None) -> dict:
    r = client.post("/auth/device-token", headers=bearer, json={"name": name} if name else None)
    assert r.status_code == 200, r.text
    return r.json()


def _screen_close_code(client: TestClient, token: str) -> int | None:
    """Open /ws/screen; the close code if it was refused, None if it was accepted."""
    with client.websocket_connect(f"/ws/screen?token={token}") as ws:
        try:
            ws.receive_text()  # the cadence message of an accepted socket
        except WebSocketDisconnect as closed:
            return closed.code
    return None


def test_minted_devices_are_listed_by_name_without_their_token(client: TestClient) -> None:
    bearer = _login(client, "op")
    first = _mint(client, bearer)
    second = _mint(client, bearer, "  MEETING-PC  ")
    listed = client.get("/auth/device-tokens", headers=bearer).json()
    assert [(d["id"], d["name"]) for d in listed] == [
        (first["id"], "Screen capture"),
        (second["id"], "MEETING-PC"),
    ]
    assert all("token" not in d and d["expiresAt"] > d["createdAt"] for d in listed)
    # Another user sees only their own.
    assert client.get("/auth/device-tokens", headers=_login(client, "other")).json() == []


def test_revoking_one_device_refuses_it_and_leaves_the_others_working(
    client: TestClient,
) -> None:
    bearer = _login(client, "op")
    kept, revoked = _mint(client, bearer, "kept"), _mint(client, bearer, "revoked")
    assert _screen_close_code(client, revoked["token"]) is None
    assert client.delete(f"/auth/device-tokens/{revoked['id']}", headers=bearer).status_code == 204
    assert _screen_close_code(client, revoked["token"]) == 1008
    assert _screen_close_code(client, kept["token"]) is None
    # The login session that revoked it is untouched, and the list drops it.
    assert [d["id"] for d in client.get("/auth/device-tokens", headers=bearer).json()] == [
        kept["id"]
    ]
    # A second revoke is "not found", as is an id that isn't one.
    assert client.delete(f"/auth/device-tokens/{revoked['id']}", headers=bearer).status_code == 404
    assert client.delete("/auth/device-tokens/nope", headers=bearer).status_code == 404


def test_revoke_closes_the_devices_open_socket(client: TestClient) -> None:
    """Auth is checked at the handshake only, so revoke must stop a live socket too."""
    bearer = _login(client, "op")
    minted = _mint(client, bearer)
    with client.websocket_connect(f"/ws/screen?token={minted['token']}") as ws:
        ws.receive_text()
        r = client.delete(f"/auth/device-tokens/{minted['id']}", headers=bearer)
        assert r.status_code == 204
        # Synchronous first, so a missing revoke fails here instead of hanging below.
        assert metrics.snapshot()["counters"].get("screen.revoked") == 1
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_text()
        assert closed.value.code == 1008
    assert screen_ws._token_revokers.get(minted["id"]) is None


def test_a_user_cannot_list_or_revoke_another_users_device(client: TestClient) -> None:
    minted = _mint(client, _login(client, "op"))
    other = _login(client, "other")
    assert client.delete(f"/auth/device-tokens/{minted['id']}", headers=other).status_code == 404
    assert _screen_close_code(client, minted["token"]) is None


def test_a_device_token_cannot_list_mint_or_revoke(client: TestClient) -> None:
    """A leaked capture-PC token must not be able to keep itself alive or see the others."""
    minted = _mint(client, _login(client, "op"))
    device = {"Authorization": f"Bearer {minted['token']}"}
    assert client.get("/auth/device-tokens", headers=device).status_code == 401
    assert client.post("/auth/device-token", headers=device).status_code == 401
    assert client.delete(f"/auth/device-tokens/{minted['id']}", headers=device).status_code == 401


def test_a_device_token_without_a_row_id_is_refused() -> None:
    """Tokens minted before XERK-2594 carry no ``jti`` and can't be revoked: re-pair."""
    from api.auth import reset_user_store

    reset_user_store()
    user = get_user_store().create("legacy", "legacy-password-1", household="default")
    legacy = issue_token(
        Principal(user.user_id, user.household, user.role, user.username, scope="screen"),
        secret=settings.auth_secret,
        ttl_seconds=60,
    )
    with pytest.raises(AuthError, match="revoked"):
        principal_from_bearer(legacy, scope="screen")
    # A forged jti (valid signature needs the secret, so only a test can) is refused too.
    forged = issue_token(
        Principal(
            user.user_id, user.household, user.role, user.username,
            scope="screen", token_id="00000000-0000-4000-8000-000000000000",
        ),
        secret=settings.auth_secret,
        ttl_seconds=60,
    )
    with pytest.raises(AuthError, match="revoked"):
        principal_from_bearer(forged, scope="screen")
    reset_user_store()


def test_a_row_of_another_user_does_not_authorize_the_token() -> None:
    """The jti must belong to the token's own user, not merely exist."""
    from api.auth import reset_user_store

    reset_user_store()
    store = get_user_store()
    owner = store.create("owner", "owner-password-1", household="default")
    thief = store.create("thief", "thief-password-1", household="default")
    row = store.create_device_token(owner.user_id, name="pc", ttl_seconds=60)
    token = issue_token(
        Principal(
            thief.user_id, thief.household, thief.role, thief.username,
            scope="screen", token_id=row.token_id,
        ),
        secret=settings.auth_secret,
        ttl_seconds=60,
    )
    with pytest.raises(AuthError):
        principal_from_bearer(token, scope="screen")
    reset_user_store()


@pytest.mark.parametrize(
    "name", ["bad\x00name", "zero​width", "x" * 81, "line\nbreak"]
)
def test_device_names_refuse_invisible_characters_and_overlong_text(
    client: TestClient, name: str
) -> None:
    r = client.post("/auth/device-token", headers=_login(client, "op"), json={"name": name})
    assert r.status_code == 422
    assert client.get("/auth/device-tokens", headers=_login(client, "op")).json() == []


def test_in_memory_store_drops_expired_and_deleted_users_tokens() -> None:
    from api.auth.users import InMemoryUserStore

    store = InMemoryUserStore()
    user = store.create("u", "u-password-123", household="default")
    expired = store.create_device_token(user.user_id, name="old", ttl_seconds=-1)
    live = store.create_device_token(user.user_id, name="new", ttl_seconds=60)
    assert [t.token_id for t in store.list_device_tokens(user.user_id)] == [live.token_id]
    assert not store.device_token_is_live(user.user_id, expired.token_id)
    with pytest.raises(KeyError):
        store.create_device_token("no-such-user", name="x", ttl_seconds=60)
    store.delete(user.user_id)
    assert not store.device_token_is_live(user.user_id, live.token_id)
    assert not store.revoke_device_token(user.user_id, live.token_id)


def test_a_revoke_between_handshake_and_registration_still_closes_the_socket(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler re-checks the row after registering its revoker: a revoke that lands
    after auth but before registration sees no socket to close, so only that check stops it."""
    bearer = _login(client, "op")
    minted = _mint(client, bearer)
    user = get_user_store().get_by_username("op")

    def revoke_mid_handshake():
        # Runs after auth and before the revoker is registered (screen_ws reads it there).
        assert get_user_store().revoke_device_token(user.user_id, minted["id"])
        return _NoOcr()

    monkeypatch.setattr(screen_ws, "get_ocr_client", revoke_mid_handshake)
    with client.websocket_connect(f"/ws/screen?token={minted['token']}") as ws:
        ws.receive_text()  # the cadence is sent either way
        # A ping answers "pong" on a socket that survived, so a regression fails here
        # instead of hanging on a close that never comes.
        ws.send_text('{"type": "ping"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008


def test_revoking_an_expired_device_is_not_found(client: TestClient) -> None:
    """Expired devices drop off the list, so revoke agrees: 404, not 204."""
    user = get_user_store().get_by_username("op")
    expired = get_user_store().create_device_token(user.user_id, name="old", ttl_seconds=-1)
    r = client.delete(f"/auth/device-tokens/{expired.token_id}", headers=_login(client, "op"))
    assert r.status_code == 404


def test_minting_is_capped_and_dead_rows_are_purged(client: TestClient) -> None:
    from api.auth.users import MAX_DEVICE_TOKENS

    bearer = _login(client, "op")
    minted = [_mint(client, bearer) for _ in range(MAX_DEVICE_TOKENS)]
    r = client.post("/auth/device-token", headers=bearer)
    assert r.status_code == 409 and "revoke one" in r.json()["detail"]
    # Revoking one frees a slot.
    client.delete(f"/auth/device-tokens/{minted[0]['id']}", headers=bearer)
    assert client.post("/auth/device-token", headers=bearer).status_code == 200
    # Another user's cap is their own.
    assert client.post("/auth/device-token", headers=_login(client, "other")).status_code == 200


def test_expired_rows_are_purged_on_the_next_mint() -> None:
    from api.auth.users import InMemoryUserStore

    store = InMemoryUserStore()
    user = store.create("u", "u-password-123", household="default")
    dead = store.create_device_token(user.user_id, name="old", ttl_seconds=-1)
    store.create_device_token(user.user_id, name="new", ttl_seconds=60)
    assert dead.token_id not in store._device_tokens
