"""Pluggable auth backend: coexistence, the operator toggle, and config discovery
(XERK-652, T6).

Built-in username/password is the default and never goes away; Authentik OIDC is an
opt-in second backend gated by ``API_OIDC_ENABLED``. This file covers the T6-owned
seams end-to-end through ``TestClient``:

- ``GET /auth/config`` — the public advertisement that lets a client decide whether to
  show the OIDC button. It reflects the flag: OIDC off ⇒ ``{"builtin": true}`` only;
  OIDC on ⇒ an ``oidc`` block with issuer/clientId/scopes.
- Coexistence: a local (built-in) login works in BOTH flag states — so a local admin
  can still get in even if Authentik is down — while an OIDC token authenticates only
  when the flag is on.
- The three principal kinds the policy must serve, via conftest's ``local_actor`` /
  ``oidc_actor`` / ``linked_actor`` fixtures.

The verifier units, boot guard, and account-linking order live in ``test_oidc.py`` /
``test_oidc_provisioning.py``; this file is the toggle + discovery + coexistence layer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import (
    OIDC_AUDIENCE,
    OIDC_HOUSEHOLD,
    OIDC_ISSUER,
    AuthActor,
    OidcEnv,
)
from api.config import settings
from api.main import app


# --- config discovery: the advertisement reflects the flag --------------------


@pytest.mark.real_auth
def test_auth_config_oidc_off_advertises_builtin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default deployment: built-in only. The ``oidc`` block is absent entirely, so a
    client shows just the username/password form and behaves exactly as before."""
    monkeypatch.setattr(settings, "auth_secret", "test-secret-0123456789abcdef0123456789")
    monkeypatch.setattr(settings, "oidc_enabled", False)
    with TestClient(app) as client:
        body = client.get("/auth/config").json()
    assert body == {"builtin": True}


@pytest.mark.real_auth
def test_auth_config_reflects_flag_when_oidc_on(oidc_env: OidcEnv) -> None:
    """OIDC on: the advertisement carries the issuer + client_id (and default scopes) a
    client needs to start Authorization Code + PKCE. ``authorizationEndpoint`` is absent
    by default — the client re-discovers it — so it must not appear as ``null``."""
    with TestClient(app) as client:
        body = client.get("/auth/config").json()
    assert body["builtin"] is True
    assert body["oidc"]["enabled"] is True
    assert body["oidc"]["issuer"] == OIDC_ISSUER
    assert body["oidc"]["clientId"] == OIDC_AUDIENCE
    assert body["oidc"]["scopes"] == ["openid", "email", "profile", "groups"]
    assert "authorizationEndpoint" not in body["oidc"]


@pytest.mark.real_auth
def test_auth_config_advertises_pinned_endpoint_and_scopes(
    oidc_env: OidcEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator may pin the authorize endpoint (saving the discovery round-trip) and
    customise the requested scopes; both ride the advertisement when set."""
    monkeypatch.setattr(
        settings, "oidc_authorization_endpoint", "https://authentik.test/application/o/authorize/"
    )
    monkeypatch.setattr(settings, "oidc_scopes", "openid,email")
    with TestClient(app) as client:
        oidc = client.get("/auth/config").json()["oidc"]
    assert oidc["authorizationEndpoint"] == "https://authentik.test/application/o/authorize/"
    assert oidc["scopes"] == ["openid", "email"]


@pytest.mark.real_auth
def test_auth_config_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery must work before the user has any token (that is its whole job), so it
    is unauthenticated like ``/health`` — no bearer required, no 401."""
    monkeypatch.setattr(settings, "auth_secret", "test-secret-0123456789abcdef0123456789")
    monkeypatch.setattr(settings, "oidc_enabled", False)
    with TestClient(app) as client:
        assert client.get("/auth/config").status_code == 200


# --- coexistence: local login works in BOTH flag states -----------------------


def _login(client: TestClient, username: str, password: str):
    return client.post("/auth/login", json={"username": username, "password": password})


@pytest.mark.real_auth
def test_local_login_works_with_oidc_off(local_actor: AuthActor) -> None:
    """The unchanged default path: username/password login issues a token that
    authenticates. (``local_actor`` seeds the account; OIDC stays off.)"""
    with TestClient(app) as client:
        resp = _login(client, "local-admin", "local-password")
        assert resp.status_code == 200
        token = resp.json()["token"]
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "local-admin" and me.json()["role"] == "admin"


@pytest.mark.real_auth
def test_local_login_still_works_with_oidc_on(oidc_env: OidcEnv) -> None:
    """The coexistence guarantee that matters most operationally: with Authentik turned
    on, a local admin can STILL log in with built-in credentials — so they are not
    locked out if the IdP is down (ticket acceptance)."""
    from api.auth import get_user_store

    get_user_store().create(
        "local-admin", "local-password", household=OIDC_HOUSEHOLD, role="admin"
    )
    with TestClient(app) as client:
        resp = _login(client, "local-admin", "local-password")
        assert resp.status_code == 200
        me = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {resp.json()['token']}"}
        )
    assert me.status_code == 200 and me.json()["username"] == "local-admin"


@pytest.mark.real_auth
def test_oidc_token_rejected_when_oidc_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag is a real gate: with OIDC off, a JWS-shaped bearer is never routed to
    the OIDC verifier — it falls through to the built-in decoder, fails, and 401s. So
    turning the flag off genuinely removes the OIDC path (ticket acceptance)."""
    monkeypatch.setattr(settings, "auth_secret", "test-secret-0123456789abcdef0123456789")
    monkeypatch.setattr(settings, "oidc_enabled", False)
    # A three-segment (JWS-shaped) token: only the OIDC path would ever accept it.
    jws_shaped = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"
    with TestClient(app) as client:
        assert (
            client.get("/auth/me", headers={"Authorization": f"Bearer {jws_shaped}"}).status_code
            == 401
        )


# --- the three principal kinds the policy serves (conftest fixtures) ----------


@pytest.mark.real_auth
def test_local_actor_principal(local_actor: AuthActor) -> None:
    with TestClient(app) as client:
        me = client.get("/auth/me", headers=local_actor.headers)
    assert me.status_code == 200
    assert me.json()["userId"] == local_actor.user_id


@pytest.mark.real_auth
def test_oidc_actor_principal_is_jit_provisioned(oidc_actor: AuthActor) -> None:
    """An OIDC token with no matching local row authenticates and provisions a distinct
    OIDC-only account (its id is not known up front — /auth/me reveals it)."""
    from api.auth import get_user_store

    with TestClient(app) as client:
        me = client.get("/auth/me", headers=oidc_actor.headers)
    assert me.status_code == 200
    assert me.json()["household"] == OIDC_HOUSEHOLD
    provisioned = get_user_store().get_by_oidc_sub("jit-sub")
    assert provisioned is not None and provisioned.user_id == me.json()["userId"]


@pytest.mark.real_auth
def test_oidc_token_without_tenir_group_is_denied(oidc_env: OidcEnv) -> None:
    """The access gate end-to-end (docs/auth-oidc.md §7): a fully valid Authentik token
    that is in neither Tenir group 401s at the request layer and provisions no account —
    so removing a user from the Tenir group in Authentik revokes their access, and a
    non-Tenir Authentik user can never authenticate into the household."""
    from api.auth import get_user_store

    token = oidc_env.mint(sub="outsider", email="outsider@household.test", groups=[])
    with TestClient(app) as client:
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 401
    assert get_user_store().get_by_oidc_sub("outsider") is None  # nothing provisioned


@pytest.mark.real_auth
def test_linked_actor_resolves_to_existing_local_row(linked_actor: AuthActor) -> None:
    """A linked account authenticates through OIDC to the SAME local id as the row that
    owned the verified email — one identity, no duplicate (docs/auth-oidc.md §5, §8)."""
    from api.auth import get_user_store

    with TestClient(app) as client:
        me = client.get("/auth/me", headers=linked_actor.headers).json()
    assert me["userId"] == linked_actor.user_id  # same row, not a new one
    # The single local row now carries the Authentik subject.
    assert get_user_store().get_by_oidc_sub("linked-sub").user_id == linked_actor.user_id
    assert len(get_user_store().list_by_household(OIDC_HOUSEHOLD)) == 1
