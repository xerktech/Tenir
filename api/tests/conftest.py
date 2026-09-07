"""Shared test fixtures."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from api import registry

# Long enough to satisfy the boot guard's minimum (XERK-236: an empty or
# one-character API_AUTH_SECRET used to boot, which makes tokens forgeable).
TEST_AUTH_SECRET = "test-secret-0123456789abcdef0123456789"

# Fixed Authentik-shaped issuer/audience/household the OIDC test fixtures below use.
OIDC_ISSUER = "https://authentik.test/application/o/tenir/"
OIDC_AUDIENCE = "tenir-test-client"
OIDC_HOUSEHOLD = "default"


@pytest.fixture(autouse=True)
def _auth(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth is always required (there is no no-login mode).

    A real signing secret is set so the app boots under ``TestClient`` (the startup
    guard refuses the insecure default). Most tests don't care about *who* the caller
    is, so by default we seed an admin and override the principal-resolving
    dependencies — the WS resolver included — so existing token-less calls resolve to
    that admin. Tests that exercise auth itself (401s, role gating, household
    isolation) opt out with ``@pytest.mark.real_auth`` and present real tokens.
    """
    from api import main
    from api.auth import Principal, get_user_store, reset_user_store
    from api.auth.deps import current_principal, principal_from_request
    from api.config import settings
    from api.main import app

    monkeypatch.setattr(settings, "auth_secret", TEST_AUTH_SECRET)
    if request.node.get_closest_marker("real_auth"):
        yield
        return

    reset_user_store()
    admin = get_user_store().create(
        "test-admin", "test-admin-password", household=settings.household_id, role="admin"
    )
    principal = Principal(
        user_id=admin.user_id,
        username="test-admin",
        household=settings.household_id,
        role="admin",
    )
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[principal_from_request] = lambda: principal
    monkeypatch.setattr(main, "_ws_principal", lambda ws: principal)
    yield
    app.dependency_overrides.pop(current_principal, None)
    app.dependency_overrides.pop(principal_from_request, None)
    reset_user_store()


# --- coexistence auth actors (XERK-652, T6) ----------------------------------
#
# One shared place to produce each of the three principal kinds the pluggable-auth
# coexistence policy has to serve — a built-in (HMAC) local token, an Authentik OIDC
# access token, and an OIDC token that links to a *pre-existing* local row by verified
# email (docs/auth-oidc.md §5). Real tokens run through the app's own resolvers, so a
# test presents ``actor.headers`` and exercises the true code path. These are for
# ``@pytest.mark.real_auth`` tests (conftest's auto-authenticated admin override is
# skipped there); each manages the user store itself so it is self-contained.


def _rsa_keypair(kid: str) -> tuple[str, dict]:
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


@dataclass
class AuthActor:
    """A ready-to-use authenticated caller: its bearer token and the header carrying it.

    ``user_id`` is the local ``users.id`` the token resolves to when it is known up
    front (built-in and linked rows); it is ``None`` for a JIT OIDC token, whose row is
    provisioned on first use and whose id the test reads back from ``/auth/me``.
    """

    kind: str  # "local" | "oidc" | "linked"
    token: str
    user_id: str | None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class OidcEnv:
    """The OIDC backend turned on for a test, plus a token minter for its wired key."""

    mint: Callable[..., str]  # mint(**claim_overrides) -> a signed access token


@pytest.fixture
def oidc_env(monkeypatch: pytest.MonkeyPatch):
    """Turn the optional OIDC backend on and wire the app's verifier to a local RSA
    keypair, so real Authentik-shaped access tokens validate offline (no network, no
    live Authentik). Yields a minter for signed tokens; linking/JIT still runs through
    the normal request path. Resets the user store around the test so it starts clean.
    """
    from api.auth import deps, reset_user_store
    from api.auth.oidc import OidcVerifier
    from api.config import settings

    kid = "test-key-1"
    private_pem, jwk = _rsa_keypair(kid)
    monkeypatch.setattr(settings, "auth_secret", TEST_AUTH_SECRET)
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", OIDC_ISSUER)
    monkeypatch.setattr(settings, "oidc_audience", OIDC_AUDIENCE)
    monkeypatch.setattr(settings, "auth_admin_household", OIDC_HOUSEHOLD)

    verifier = OidcVerifier(
        issuer=OIDC_ISSUER,
        audience=OIDC_AUDIENCE,
        jwks_url=settings.oidc_jwks_url_resolved,
        algorithms=["RS256"],
        groups_claim=settings.oidc_groups_claim,
        admin_group=settings.oidc_admin_group,
        email_claim=settings.oidc_email_claim,
        email_verified_claim=settings.oidc_email_verified_claim,
        household=OIDC_HOUSEHOLD,
        fetch_jwks=lambda: {"keys": [jwk]},
    )
    monkeypatch.setattr(deps, "_oidc_verifier", verifier)
    reset_user_store()

    def mint(**claims: object) -> str:
        now = int(time.time())
        payload: dict[str, object] = {
            "iss": OIDC_ISSUER,
            "aud": OIDC_AUDIENCE,
            "sub": "authentik-user",
            "email": "user@household.test",
            "email_verified": True,
            "groups": ["tenir-members"],
            "iat": now,
            "exp": now + 300,
        }
        payload.update(claims)
        return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})

    yield OidcEnv(mint=mint)
    monkeypatch.setattr(deps, "_oidc_verifier", None)
    reset_user_store()


@pytest.fixture
def local_actor(monkeypatch: pytest.MonkeyPatch):
    """A built-in (HMAC) local-account principal — a username/password admin. Works the
    same whether OIDC is on or off, which is the whole point of coexistence."""
    from api.auth import Principal, get_user_store, issue_token, reset_user_store
    from api.config import settings

    monkeypatch.setattr(settings, "auth_secret", TEST_AUTH_SECRET)
    reset_user_store()
    user = get_user_store().create(
        "local-admin", "local-password", household=OIDC_HOUSEHOLD, role="admin"
    )
    token = issue_token(
        Principal(user.user_id, OIDC_HOUSEHOLD, "admin", username="local-admin"),
        secret=TEST_AUTH_SECRET,
        ttl_seconds=300,
    )
    yield AuthActor(kind="local", token=token, user_id=user.user_id)
    reset_user_store()


@pytest.fixture
def oidc_actor(oidc_env: OidcEnv) -> AuthActor:
    """An OIDC-token principal with no pre-existing local row: the validated token
    JIT-provisions a fresh OIDC-only account on first use (docs/auth-oidc.md §5)."""
    token = oidc_env.mint(sub="jit-sub", email="jit@household.test", email_verified=True)
    return AuthActor(kind="oidc", token=token, user_id=None)


@pytest.fixture
def linked_actor(oidc_env: OidcEnv) -> AuthActor:
    """A linked account: a pre-created *local* row whose verified email matches the OIDC
    token, so the token resolves to that same row — same id, role, and owned recordings
    — instead of a duplicate (docs/auth-oidc.md §5). ``user_id`` is that local row's id."""
    from api.auth import get_user_store

    user = get_user_store().create(
        "linkme",
        "local-password",
        household=OIDC_HOUSEHOLD,
        role="member",
        email="linkme@household.test",
    )
    token = oidc_env.mint(
        sub="linked-sub", email="linkme@household.test", email_verified=True,
        groups=["tenir-members"],
    )
    return AuthActor(kind="linked", token=token, user_id=user.user_id)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    # Sessions linger in the registry after a socket drop (resume grace window);
    # clear it around every test so one test's live/detached sessions never leak
    # into another's /health or resume lookups.
    registry._active.clear()
    yield
    registry._active.clear()
