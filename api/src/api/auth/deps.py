"""Request-scoping dependencies (master plan §7, Phase 6).

``current_principal`` is the single place household tenancy is resolved for the
REST API. Auth is always required: a valid bearer token must accompany every
request and the household comes from the token, so a user can never read another
household's data by guessing its id.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from api.auth.oidc import OidcVerifier
from api.auth.tokens import AuthError, Principal, decode_token
from api.auth.users import get_user_store
from api.config import DEFAULT_AUTH_SECRET, settings


# Shortest signing secret the api will boot with. A bearer token is only as hard
# to forge as the HMAC key is to guess, and these tokens carry the household and
# the admin role — so anything brute-forceable is the same hole as the shipped
# default. 32 chars is `openssl rand -hex 16`, the smallest thing anyone
# generating a secret properly will produce.
MIN_AUTH_SECRET_LENGTH = 32


def assert_secure_auth_config() -> None:
    """Refuse to boot on a signing secret that isn't one.

    A forged token is only as hard as the secret is secret (master plan §7); a
    deployment that runs without a real ``API_AUTH_SECRET`` lets anyone mint an
    admin token for any household. Checking only against the shipped default
    was not enough — an empty, blank or one-character value passed that check
    and booted, and an empty HMAC key is trivially forgeable (XERK-236). Called
    at api startup.
    """
    secret = settings.auth_secret
    if secret == DEFAULT_AUTH_SECRET:
        raise RuntimeError(
            "API_AUTH_SECRET is still the insecure default; set a strong "
            "API_AUTH_SECRET before starting the api."
        )
    if not secret.strip():
        raise RuntimeError(
            "API_AUTH_SECRET is empty; set a strong API_AUTH_SECRET before starting the api."
        )
    if len(secret) < MIN_AUTH_SECRET_LENGTH:
        raise RuntimeError(
            f"API_AUTH_SECRET is too short ({len(secret)} chars); use at least "
            f"{MIN_AUTH_SECRET_LENGTH} — e.g. `openssl rand -hex 32`."
        )


def assert_valid_oidc_config() -> None:
    """Refuse to boot on a half-configured OIDC backend (XERK-649, T3).

    When OIDC is off nothing new is required. When it is on, a validator that can't
    name its issuer/audience would silently accept nothing (or, worse, skip a
    check) — the same latent-hole class ``assert_secure_auth_config`` guards. So we
    fail fast, mirroring that guard. Called at api startup.
    """
    if not settings.oidc_enabled:
        return
    missing = [
        name
        for name, value in (
            ("API_OIDC_ISSUER", settings.oidc_issuer),
            ("API_OIDC_AUDIENCE", settings.oidc_audience),
        )
        if not value.strip()
    ]
    if missing:
        raise RuntimeError(
            "API_OIDC_ENABLED is true but "
            + ", ".join(missing)
            + " is not set; OIDC cannot validate tokens without an issuer and audience."
        )
    if not settings.oidc_jwks_url_resolved:
        raise RuntimeError("API_OIDC_ENABLED is true but no JWKS URL could be resolved.")
    algs = settings.oidc_algorithm_list
    if not algs:
        raise RuntimeError("API_OIDC_ENABLED is true but API_OIDC_ALGORITHMS is empty.")
    # RSA-family only. Authentik signs with RS256, and the verifier loads RSA JWKS
    # keys — allowing a symmetric alg (HS*) here is a misconfiguration that would
    # feed an RSA key object into an HMAC verify and 500 on a forged token rather
    # than fail clean; and it is the shape alg-confusion attacks exploit. Refuse it
    # at boot so the mistake surfaces immediately, not deep in a request.
    non_rsa = [a for a in algs if not a.startswith("RS")]
    if non_rsa:
        raise RuntimeError(
            f"API_OIDC_ALGORITHMS must be RSA-family (RS256/RS384/RS512); got {non_rsa}."
        )


# One verifier per process holds the JWKS cache, so it survives Authentik key
# rotation without a restart (a rotated-in key misses the cache once and refetches).
_oidc_verifier: OidcVerifier | None = None


def get_oidc_verifier() -> OidcVerifier:
    """The process-wide OIDC verifier, built from settings on first use."""
    global _oidc_verifier
    if _oidc_verifier is None:
        _oidc_verifier = OidcVerifier.from_settings(settings)
    return _oidc_verifier


def reset_oidc_verifier() -> None:
    """Drop the cached verifier (tests that reconfigure OIDC settings)."""
    global _oidc_verifier
    _oidc_verifier = None


def principal_from_token(token: str) -> Principal:
    """Decode a bearer token to a Principal, raising AuthError if it is invalid."""
    return decode_token(token, secret=settings.auth_secret)


def _is_oidc_token(token: str) -> bool:
    """Discriminate an OIDC JWS from a built-in HMAC token by segment count.

    Built-in token = ``<payload>.<hmac-sig>`` — exactly two dot-separated segments.
    An OIDC access token is a standard JWS — three (``header.payload.signature``).
    The shapes cannot be confused (docs/auth-oidc.md §1), and each verifier only
    ever runs its own algorithm, so there is no alg-confusion path between them.
    """
    return token.count(".") == 2


def principal_from_bearer(token: str) -> Principal:
    """Resolve a bearer to a Principal via whichever backend accepts it.

    A built-in HMAC token goes through :func:`principal_from_live_token` exactly as
    before (signature + expiry + account liveness). When OIDC is enabled, a
    three-segment JWS is verified against Authentik's JWKS instead. Both backends
    coexist — a request is authenticated by whichever one accepts its token — and
    both yield the same :class:`Principal` seam. Raises ``AuthError`` on rejection.
    """
    if settings.oidc_enabled and _is_oidc_token(token):
        return get_oidc_verifier().verify(token)
    return principal_from_live_token(token)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def principal_from_live_token(token: str) -> Principal:
    """Decode a bearer token AND check the account behind it still exists.

    Deleting a user has to end their access now, not whenever their token
    happens to expire. Tokens are stateless and long-lived (30 days by default,
    and sliding renewal keeps an active device's token fresh indefinitely), so
    without this lookup a removed member kept full household access for up to a
    month after being deleted (XERK-236). One store read per authenticated
    request is the price of revocation actually revoking.
    """
    principal = principal_from_token(token)
    if get_user_store().get_by_id(principal.user_id) is None:
        raise AuthError("account no longer exists")
    return principal


def current_principal(
    authorization: str | None = Header(default=None),
) -> Principal:
    """Resolve the authenticated principal for a REST request."""
    token = _bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return principal_from_bearer(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def principal_from_request(
    authorization: str | None = Header(default=None),
    token: str | None = None,
) -> Principal:
    """Resolve the principal for endpoints reached by plain browser navigation.

    Audio download/playback is opened via an ``<a href>`` / ``Linking.openURL``,
    which cannot set an ``Authorization`` header — so this also accepts the token as
    a ``?token=`` query param (like the WS handler). Identical to
    ``current_principal`` otherwise: a valid token is required and the household
    comes from it.
    """
    tok = _bearer(authorization) or token
    if tok is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return principal_from_bearer(tok)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    """Like ``current_principal`` but 403s non-admins (master capture toggle, etc.)."""
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin role required")
    return principal
