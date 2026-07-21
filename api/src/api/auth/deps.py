"""Request-scoping dependencies (master plan §7, Phase 6).

``current_principal`` is the single place household tenancy is resolved for the
REST API. Auth is always required: a valid bearer token must accompany every
request and the household comes from the token, so a user can never read another
household's data by guessing its id.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from api.auth.tokens import AuthError, Principal, decode_token
from api.config import DEFAULT_AUTH_SECRET, settings


def assert_secure_auth_config() -> None:
    """Refuse to boot while the signing secret is the shipped default.

    A forged token is only as hard as the secret is secret (master plan §7); a
    deployment that runs without overriding ``API_AUTH_SECRET`` would let anyone
    mint an admin token for any household. Called at api startup.
    """
    if settings.auth_secret == DEFAULT_AUTH_SECRET:
        raise RuntimeError(
            "API_AUTH_SECRET is still the insecure default; set a strong "
            "API_AUTH_SECRET before starting the api."
        )


def principal_from_token(token: str) -> Principal:
    """Decode a bearer token to a Principal, raising AuthError if it is invalid."""
    return decode_token(token, secret=settings.auth_secret)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def current_principal(
    authorization: str | None = Header(default=None),
) -> Principal:
    """Resolve the authenticated principal for a REST request."""
    token = _bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return principal_from_token(token)
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
        return principal_from_token(tok)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    """Like ``current_principal`` but 403s non-admins (master capture toggle, etc.)."""
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin role required")
    return principal
