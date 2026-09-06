"""Optional Authentik OIDC access-token validation (XERK-649, T3).

The API as an OAuth2 resource server: a second auth backend that validates
Authentik JWTs (RS256, against JWKS) alongside — never replacing — the built-in
HMAC tokens. Everything here runs offline against a locally-generated RSA keypair
(pyjwt[crypto] is a base dep), so CI needs no network and no live Authentik.

Covers the verifier units the ticket calls for — good / bad-sig / expired /
wrong-aud / wrong-iss / unknown-kid→refetch, groups→role, email extraction — plus
the coexistence guarantees (built-in tokens still authenticate with OIDC on and
off) and the boot guard.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from api.auth import Principal, issue_token
from api.auth.deps import assert_valid_oidc_config
from api.auth.oidc import OidcVerifier
from api.auth.tokens import AuthError
from conftest import TEST_AUTH_SECRET
from api.config import settings
from api.main import app

ISSUER = "https://authentik.test/application/o/tenir/"
AUDIENCE = "tenir"
HOUSEHOLD = "default"


# --- key / token / JWKS helpers ----------------------------------------------


def _keypair(kid: str) -> tuple[str, dict]:
    """A fresh RSA keypair as (private PEM, public JWK dict tagged with ``kid``)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return private_pem, jwk


def _token(private_pem: str, kid: str, **claims: object) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "authentik-user-1",
        "email": "user@household.test",
        "email_verified": True,
        "groups": ["tenir-members"],
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


def _verifier(jwks_keys: list[dict], *, fetch_calls: list | None = None) -> OidcVerifier:
    """A verifier whose JWKS 'fetch' returns ``jwks_keys`` (no network)."""

    def fetch() -> dict:
        if fetch_calls is not None:
            fetch_calls.append(True)
        return {"keys": list(jwks_keys)}

    return OidcVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://authentik.test/application/o/tenir/jwks/",
        algorithms=["RS256"],
        groups_claim="groups",
        admin_group="tenir-admins",
        email_claim="email",
        email_verified_claim="email_verified",
        household=HOUSEHOLD,
        leeway_seconds=60,
        fetch_jwks=fetch,
    )


# --- verifier: the happy path and claim extraction ---------------------------


def test_valid_token_maps_to_principal() -> None:
    priv, jwk = _keypair("k1")
    v = _verifier([jwk])
    p = v.verify(_token(priv, "k1", sub="abc", email="maya@acme.test", groups=["tenir-members"]))
    assert p.sub == "abc"
    assert p.user_id == "abc"  # T3 interim: subject is the id until T4 resolves it
    assert p.email == "maya@acme.test"
    assert p.email_verified is True
    assert p.role == "member"
    assert p.household == HOUSEHOLD
    assert p.groups == ("tenir-members",)


def test_groups_map_to_role() -> None:
    priv, jwk = _keypair("k1")
    v = _verifier([jwk])
    assert v.verify(_token(priv, "k1", groups=["tenir-admins"])).role == "admin"
    assert v.verify(_token(priv, "k1", groups=["tenir-admins", "other"])).role == "admin"
    assert v.verify(_token(priv, "k1", groups=["tenir-members"])).role == "member"
    assert v.verify(_token(priv, "k1", groups=[])).role == "member"
    assert v.verify(_token(priv, "k1", groups="not-a-list")).role == "member"
    # Absent groups claim → plain member (a validated token is at least a member).
    assert v.verify(_token(priv, "k1", groups=None)).role == "member"


def test_email_verified_extraction_is_strict_boolean() -> None:
    """Only a literal boolean true counts as verified — the T4 link guard must not
    be tricked by a truthy string or a missing claim (docs/auth-oidc.md §5)."""
    priv, jwk = _keypair("k1")
    v = _verifier([jwk])
    assert v.verify(_token(priv, "k1", email_verified=True)).email_verified is True
    assert v.verify(_token(priv, "k1", email_verified=False)).email_verified is False
    assert v.verify(_token(priv, "k1", email_verified="true")).email_verified is False
    assert v.verify(_token(priv, "k1", email_verified=1)).email_verified is False
    # Absent email_verified → False, and an absent email → None (not a crash).
    p = v.verify(_token(priv, "k1", **{"email_verified": None, "email": None}))
    assert p.email_verified is False and p.email is None


# --- verifier: rejections (each ⇒ AuthError ⇒ 401) ---------------------------


def test_bad_signature_rejected() -> None:
    priv, jwk = _keypair("k1")
    other_priv, _ = _keypair("k1")  # same kid, different (attacker) key
    v = _verifier([jwk])
    with pytest.raises(AuthError):
        v.verify(_token(other_priv, "k1"))


def test_expired_token_rejected() -> None:
    priv, jwk = _keypair("k1")
    v = _verifier([jwk])
    now = int(time.time())
    with pytest.raises(AuthError, match="invalid OIDC token"):
        v.verify(_token(priv, "k1", iat=now - 3600, exp=now - 3000))


def test_wrong_audience_rejected() -> None:
    priv, jwk = _keypair("k1")
    v = _verifier([jwk])
    with pytest.raises(AuthError):
        v.verify(_token(priv, "k1", aud="some-other-client"))
    # aud as an array that does not contain our audience is also rejected...
    with pytest.raises(AuthError):
        v.verify(_token(priv, "k1", aud=["a", "b"]))
    # ...but an array that DOES contain it passes (Authentik can issue multi-aud).
    assert v.verify(_token(priv, "k1", aud=[AUDIENCE, "extra"])).sub == "authentik-user-1"


def test_wrong_issuer_rejected() -> None:
    priv, jwk = _keypair("k1")
    v = _verifier([jwk])
    with pytest.raises(AuthError):
        v.verify(_token(priv, "k1", iss="https://evil.test/application/o/tenir/"))


def test_alg_none_and_missing_kid_rejected() -> None:
    priv, jwk = _keypair("k1")
    v = _verifier([jwk])
    # An unsigned token (alg: none) must never validate.
    now = int(time.time())
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": now + 300},
        key=None,
        algorithm="none",
    )
    with pytest.raises(AuthError, match="alg"):
        v.verify(unsigned)
    # A token with no kid header can't be matched to a signing key.
    no_kid = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": now + 300},
        priv,
        algorithm="RS256",
    )
    with pytest.raises(AuthError, match="kid"):
        v.verify(no_kid)


def test_missing_sub_rejected() -> None:
    priv, jwk = _keypair("k1")
    v = _verifier([jwk])
    with pytest.raises(AuthError, match="sub"):
        v.verify(_token(priv, "k1", sub=""))


def test_malformed_token_rejected() -> None:
    priv, jwk = _keypair("k1")
    v = _verifier([jwk])
    with pytest.raises(AuthError, match="malformed"):
        v.verify("not.a.jwt")


# --- verifier: JWKS caching & rotation ---------------------------------------


def test_unknown_kid_triggers_refetch_for_rotation() -> None:
    """A rotated-in Authentik key is unknown to the cache exactly once; the miss
    refetches the JWKS and the next verify succeeds — rotation with no restart."""
    priv1, jwk1 = _keypair("k1")
    priv2, jwk2 = _keypair("k2")
    served = [jwk1]  # the JWKS currently published by Authentik
    calls: list = []
    v = _verifier(served, fetch_calls=calls)

    # First verify with k1 populates the cache (one fetch).
    assert v.verify(_token(priv1, "k1")).sub == "authentik-user-1"
    assert len(calls) == 1

    # Authentik rotates in k2. A k2 token misses the cache → one refetch → succeeds.
    served.append(jwk2)
    assert v.verify(_token(priv2, "k2")).sub == "authentik-user-1"
    assert len(calls) == 2

    # A cached key needs no further fetch.
    assert v.verify(_token(priv1, "k1")).sub == "authentik-user-1"
    assert len(calls) == 2


def test_truly_unknown_kid_refetches_once_then_rejects() -> None:
    priv1, jwk1 = _keypair("k1")
    priv_ghost, _ = _keypair("ghost")  # never published in the JWKS
    calls: list = []
    v = _verifier([jwk1], fetch_calls=calls)
    with pytest.raises(AuthError, match="unknown OIDC signing key"):
        v.verify(_token(priv_ghost, "ghost"))
    assert len(calls) == 1  # refetched once, did not loop


def test_jwks_fetch_failure_is_401_not_500() -> None:
    def boom() -> dict:
        raise RuntimeError("jwks server down")

    v = OidcVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://authentik.test/jwks/",
        algorithms=["RS256"],
        groups_claim="groups",
        admin_group="tenir-admins",
        email_claim="email",
        email_verified_claim="email_verified",
        household=HOUSEHOLD,
        fetch_jwks=boom,
    )
    priv, _ = _keypair("k1")
    with pytest.raises(AuthError, match="could not load OIDC signing keys"):
        v.verify(_token(priv, "k1"))


# --- boot guard --------------------------------------------------------------


def test_oidc_boot_guard_off_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_enabled", False)
    assert_valid_oidc_config()  # never raises when OIDC is off


def test_oidc_boot_guard_requires_issuer_and_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "")
    monkeypatch.setattr(settings, "oidc_audience", "")
    with pytest.raises(RuntimeError, match="API_OIDC_ISSUER"):
        assert_valid_oidc_config()
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    with pytest.raises(RuntimeError, match="API_OIDC_AUDIENCE"):
        assert_valid_oidc_config()
    monkeypatch.setattr(settings, "oidc_audience", AUDIENCE)
    assert_valid_oidc_config()  # both set → boots


def test_jwks_url_derives_from_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_jwks_url", "")
    assert settings.oidc_jwks_url_resolved == f"{ISSUER}jwks/"
    monkeypatch.setattr(settings, "oidc_jwks_url", "https://front/keys")
    assert settings.oidc_jwks_url_resolved == "https://front/keys"


# --- integration: REST + WS coexistence (built-in AND OIDC) ------------------


@pytest.fixture
def oidc_enabled(monkeypatch: pytest.MonkeyPatch):
    """Enable OIDC with a locally-keyed verifier wired into the deps singleton."""
    from api.auth import deps

    priv, jwk = _keypair("k1")
    monkeypatch.setattr(settings, "auth_secret", TEST_AUTH_SECRET)
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_audience", AUDIENCE)
    monkeypatch.setattr(settings, "auth_admin_household", HOUSEHOLD)
    monkeypatch.setattr(deps, "_oidc_verifier", _verifier([jwk]))
    yield priv
    monkeypatch.setattr(deps, "_oidc_verifier", None)


@pytest.mark.real_auth
def test_oidc_token_authenticates_rest(oidc_enabled: str) -> None:
    priv = oidc_enabled
    token = _token(priv, "k1", sub="maya-oidc", email="maya@acme.test", groups=["tenir-admins"])
    with TestClient(app) as client:
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["role"] == "admin"
        assert me.json()["household"] == HOUSEHOLD


@pytest.mark.real_auth
def test_invalid_oidc_token_is_401(oidc_enabled: str) -> None:
    other_priv, _ = _keypair("k1")  # not the served key → bad signature
    bad = _token(other_priv, "k1")
    with TestClient(app) as client:
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


@pytest.mark.real_auth
def test_builtin_token_still_works_with_oidc_on(oidc_enabled: str) -> None:
    from api.auth import get_user_store

    user = get_user_store().create("local", "longpassword", household="acme", role="admin")
    token = issue_token(
        Principal(user.user_id, "acme", "admin", username="local"),
        secret=TEST_AUTH_SECRET,
        ttl_seconds=300,
    )
    with TestClient(app) as client:
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["username"] == "local" and me.json()["household"] == "acme"


@pytest.mark.real_auth
def test_oidc_and_builtin_tokens_over_ws(oidc_enabled: str) -> None:
    import json as _json

    from starlette.websockets import WebSocketDisconnect

    from api.auth import get_user_store

    priv = oidc_enabled
    oidc_token = _token(priv, "k1", sub="ws-oidc")
    user = get_user_store().create("wslocal", "longpassword", household=HOUSEHOLD)
    builtin = issue_token(
        Principal(user.user_id, HOUSEHOLD, "member"), secret=TEST_AUTH_SECRET, ttl_seconds=300
    )
    start = _json.dumps({"type": "session.start", "micSource": "phone-microphone"})
    with TestClient(app) as client:
        for token in (oidc_token, builtin):
            with client.websocket_connect(f"/ws?token={token}") as ws:
                ws.send_text(start)
                assert ws.receive_json()["type"] == "session.ready"
        # A bad OIDC token still closes 1008, exactly as a bad built-in token does.
        bad_priv, _ = _keypair("k1")
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ws?token={_token(bad_priv, 'k1')}") as ws:
                ws.receive_json()
        assert exc.value.code == 1008
