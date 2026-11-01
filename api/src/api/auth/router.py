"""Auth REST API (master plan §7, Phase 6).

The login surface for the web UI and the Android app: exchange a username + password
for a bearer token, and read back the current principal. Admins can also create
additional household members.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from api import registry
from api.auth.deps import SCREEN_SCOPE, current_principal, require_admin
from api.auth.tokens import Principal, Role, issue_token
from api.auth.users import (
    DeviceToken,
    DuplicateUser,
    InvalidUserValue,
    get_user_store,
    is_lookup_safe,
    new_username,
)
from api.config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# A deliberately permissive check — one ``@`` with a dotted domain and no spaces —
# enough to keep junk like "aaa" or a whitespace-only string out of the roster and
# the OIDC verified-email link key (XERK-661), without pulling in a full RFC-5322
# validator. The stored value is stripped so trailing whitespace never lands in an
# email index.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validated_email(value: str | None) -> str | None:
    """Strip and shape-check an optional email; ``None`` stays ``None`` (no change)."""
    if value is None:
        return None
    stripped = value.strip()
    if not _EMAIL_RE.match(stripped) or not is_lookup_safe(stripped):
        raise ValueError("must be a valid email address")
    return stripped


def _hashable_password(value: str | None) -> str | None:
    """A lone surrogate can't be UTF-8 encoded for hashing: a 422, not a 500. A NUL is
    refused too: PBKDF2's HMAC zero-pads the key, so "pw" and "pw\\x00" hash alike."""
    if value is not None and not is_lookup_safe(value):
        raise ValueError("must be valid text")
    return value


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenOut(BaseModel):
    token: str
    tokenType: str = "bearer"
    expiresIn: int
    userId: str
    username: str
    household: str
    role: str


class PrincipalOut(BaseModel):
    userId: str
    username: str
    household: str
    role: str

    @classmethod
    def of(cls, principal: Principal) -> "PrincipalOut":
        return cls(
            userId=principal.user_id,
            username=principal.username,
            household=principal.household,
            role=principal.role,
        )


class CreateUserIn(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8, description="At least 8 characters.")
    role: Role = "member"
    # Optional link email (XERK-661): set it at creation time so the member links in
    # place to their Authentik identity by verified email on first OIDC login, rather
    # than being JIT-provisioned as a duplicate OIDC-only row (docs/auth-oidc.md §5).
    email: str | None = None

    _check_email = field_validator("email")(staticmethod(_validated_email))
    _check_password = field_validator("password")(staticmethod(_hashable_password))

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        # The stores' own check (NFKC + str.strip(), no format characters): pydantic's
        # strip_whitespace misses e.g. \x1f, which then stored an empty username
        # (XERK-1548, XERK-1591). A refusal is a 422.
        return new_username(value)


class UpdateUserIn(BaseModel):
    """Fields an admin may set on an existing user (XERK-661). All optional; at least
    one must be present.

    ``email`` is the verified-email link key for OIDC migration (set it on an existing
    local member so their Authentik login links in place). ``password`` restores local
    login — notably on an OIDC-only row after an OIDC rollback, which has no password.
    ``role`` promotes/demotes between member and admin.
    """

    email: str | None = None
    password: str | None = Field(
        default=None, min_length=8, description="At least 8 characters."
    )
    role: Role | None = None

    _check_email = field_validator("email")(staticmethod(_validated_email))
    _check_password = field_validator("password")(staticmethod(_hashable_password))


class UserSummaryOut(BaseModel):
    userId: str
    username: str
    role: str
    # The verified-email link key (docs/auth-oidc.md §5); None on a plain local row.
    email: str | None = None
    # The env-managed bootstrap admin (API_AUTH_ADMIN_*) is reconciled from env on
    # every boot, so removing it is pointless — the UI greys out its delete control.
    isEnvAdmin: bool = False


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn) -> TokenOut:
    user = get_user_store().authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid username or password")
    principal = Principal(
        user_id=user.user_id,
        username=user.username,
        household=user.household,
        role=user.role,
    )
    token = issue_token(
        principal,
        secret=settings.auth_secret,
        ttl_seconds=settings.auth_token_ttl_seconds,
    )
    return TokenOut(
        token=token,
        expiresIn=settings.auth_token_ttl_seconds,
        userId=user.user_id,
        username=user.username,
        household=user.household,
        role=user.role,
    )


# What a device is called when the pairing client names nothing.
_DEFAULT_DEVICE_NAME = "Screen capture"
_MAX_DEVICE_NAME = 80


class DeviceTokenIn(BaseModel):
    # A label for the list/revoke UI (XERK-2594); the capture client sends its hostname.
    name: str | None = None

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        # Control, format and surrogate characters would render invisibly or not at all.
        if any(unicodedata.category(ch).startswith("C") for ch in value):
            raise ValueError("device name contains invisible or control characters")
        if len(value) > _MAX_DEVICE_NAME:
            raise ValueError(f"device name is longer than {_MAX_DEVICE_NAME} characters")
        return value or None


class DeviceTokenOut(BaseModel):
    token: str
    expiresIn: int
    scope: str
    id: str


class DeviceOut(BaseModel):
    """A live (unrevoked, unexpired) screen-capture device token, without the token."""

    id: str
    name: str
    createdAt: str
    expiresAt: str

    @classmethod
    def of(cls, token: DeviceToken) -> DeviceOut:
        return cls(
            id=token.token_id,
            name=token.name,
            createdAt=_iso(token.created_at),
            expiresAt=_iso(token.expires_at),
        )


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@router.post("/device-token", response_model=DeviceTokenOut)
def device_token(
    body: DeviceTokenIn | None = None, principal: Principal = Depends(current_principal)
) -> DeviceTokenOut:
    """Mint a screen-capture device token for the caller (XERK-2505).

    The meeting-capture client on the operator's PC can't run a browser login, so the
    operator mints this once while logged in and pastes it into the client's config. It
    is bound to the caller's own user (meetings it records are theirs), authenticates only
    /ws/screen, and never slides. Each token gets a device_tokens row (XERK-2594): revoking
    the row (``DELETE /auth/device-tokens/{id}``) or deleting the user ends it.
    """
    ttl = settings.auth_device_token_ttl_seconds
    name = (body.name if body else None) or _DEFAULT_DEVICE_NAME
    try:
        row = get_user_store().create_device_token(principal.user_id, name=name, ttl_seconds=ttl)
    except KeyError as exc:  # deleted between auth and mint
        raise HTTPException(status_code=401, detail="account no longer exists") from exc
    except InvalidUserValue as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    scoped = Principal(
        user_id=principal.user_id,
        username=principal.username,
        household=principal.household,
        role=principal.role,
        scope=SCREEN_SCOPE,
        token_id=row.token_id,
    )
    return DeviceTokenOut(
        token=issue_token(scoped, secret=settings.auth_secret, ttl_seconds=ttl),
        expiresIn=ttl,
        scope=SCREEN_SCOPE,
        id=row.token_id,
    )


@router.get("/device-tokens", response_model=list[DeviceOut])
def list_device_tokens(principal: Principal = Depends(current_principal)) -> list[DeviceOut]:
    """The caller's own live screen-capture devices (XERK-2594), oldest first."""
    return [DeviceOut.of(t) for t in get_user_store().list_device_tokens(principal.user_id)]


@router.delete("/device-tokens/{token_id}", status_code=204)
async def revoke_device_token(
    token_id: str, principal: Principal = Depends(current_principal)
) -> None:
    """Revoke one of the caller's own device tokens and close its open capture sockets.

    Another user's token is a 404, not a 403, so ids don't leak across users.
    """
    revoked = await asyncio.to_thread(
        get_user_store().revoke_device_token, principal.user_id, token_id
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="device not found")
    # Imported here to keep the auth package free of the screen package at import time.
    from api.screen.ws import revoke_token

    revoke_token(token_id)


@router.get("/me", response_model=PrincipalOut)
def me(principal: Principal = Depends(current_principal)) -> PrincipalOut:
    return PrincipalOut.of(principal)


class OidcConfigOut(BaseModel):
    """The OIDC half of the public auth advertisement (docs/auth-oidc.md §10).

    ``issuer`` and ``clientId`` are the essentials the client needs; it re-discovers
    the token/end-session endpoints (and, when ``authorizationEndpoint`` is absent,
    the authorize endpoint too) from ``{issuer}/.well-known/openid-configuration``.
    """

    enabled: bool = True
    issuer: str
    clientId: str
    authorizationEndpoint: str | None = None
    scopes: list[str]


class AuthConfigOut(BaseModel):
    """What the server advertises about its auth backends (docs/auth-oidc.md §10).

    Built-in username/password is always available (``builtin`` is always true), so the
    login form always shows. The ``oidc`` block is present only when the deployment
    turned OIDC on, letting a client conditionally show the "Sign in with Authentik"
    button in addition to the form.
    """

    builtin: bool = True
    oidc: OidcConfigOut | None = None


@router.get("/config", response_model=AuthConfigOut, response_model_exclude_none=True)
def auth_config() -> AuthConfigOut:
    """Public auth-backend advertisement (unauthenticated, like ``/health``).

    OIDC off (default): ``{"builtin": true}`` — clients show only the local login form
    and behave exactly as before. OIDC on: the ``oidc`` block is included so T7–T10 can
    show the OIDC button and drive the Authorization Code + PKCE flow. Flipping
    ``API_OIDC_ENABLED`` is the only thing that changes the response — no code change or
    data migration (XERK-652)."""
    oidc: OidcConfigOut | None = None
    if settings.oidc_enabled:
        oidc = OidcConfigOut(
            enabled=True,
            issuer=settings.oidc_issuer.strip(),
            clientId=settings.oidc_audience.strip(),
            authorizationEndpoint=settings.oidc_authorization_endpoint.strip() or None,
            scopes=settings.oidc_scope_list,
        )
    return AuthConfigOut(builtin=True, oidc=oidc)


@router.get("/users", response_model=list[UserSummaryOut])
def list_users(admin: Principal = Depends(require_admin)) -> list[UserSummaryOut]:
    # The admin manages exactly their own household's roster (decision #6).
    store = get_user_store()
    env_admin = store.get_env_admin()
    env_admin_id = env_admin.user_id if env_admin else None
    return [
        UserSummaryOut(
            userId=u.user_id,
            username=u.username,
            role=u.role,
            email=u.email,
            isEnvAdmin=u.user_id == env_admin_id,
        )
        for u in store.list_by_household(admin.household)
    ]


@router.post("/users", response_model=PrincipalOut, status_code=201)
def create_user(
    body: CreateUserIn, admin: Principal = Depends(require_admin)
) -> PrincipalOut:
    # New members join the admin's household — the team boundary (decision #6).
    try:
        user = get_user_store().create(
            body.username,
            body.password,
            household=admin.household,
            role=body.role,
            email=body.email,
        )
    except DuplicateUser as exc:
        # username or (when supplied) email already taken — both surface as 409.
        raise HTTPException(
            status_code=409, detail="username or email already taken"
        ) from exc
    except InvalidUserValue as exc:
        # e.g. a character the database's encoding can't hold.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PrincipalOut.of(
        Principal(
            user_id=user.user_id,
            username=user.username,
            household=user.household,
            role=user.role,
        )
    )


@router.patch("/users/{user_id}", response_model=UserSummaryOut)
def update_user(
    user_id: str, body: UpdateUserIn, admin: Principal = Depends(require_admin)
) -> UserSummaryOut:
    """Set an existing user's email, password and/or role (XERK-661).

    Unblocks the OIDC member-migration story (docs/oidc-runbook.md §4): set a local
    member's link ``email`` so their Authentik login links in place, or set a
    ``password`` on an OIDC-only row to restore local login after an OIDC rollback.
    Scoped to the admin's own household, like delete.
    """
    if body.email is None and body.password is None and body.role is None:
        raise HTTPException(status_code=400, detail="no changes requested")
    store = get_user_store()
    target = store.get_by_id(user_id)
    # Scope to the admin's household; 404 (not 403) avoids leaking whether a user id
    # exists in another household (mirrors delete).
    if target is None or target.household != admin.household:
        raise HTTPException(status_code=404, detail="user not found")
    env_admin = store.get_env_admin()
    if env_admin is not None and env_admin.user_id == user_id:
        # The env-managed admin's username/password/email/household are reconciled from
        # API_AUTH_ADMIN_* on every boot, so an API edit would be silently reverted.
        raise HTTPException(
            status_code=409,
            detail="the env-managed admin's credentials are set via API_AUTH_ADMIN_*",
        )
    if body.role is not None and user_id == admin.user_id and body.role != admin.role:
        # Refuse self-demotion — a lone admin demoting themselves locks the household
        # out of every admin control (same lockout guard as self-delete).
        raise HTTPException(status_code=400, detail="you cannot change your own role")
    updated = target
    try:
        if body.email is not None or body.password is not None:
            updated = store.update_credentials(
                user_id, email=body.email, password=body.password
            )
        if body.role is not None:
            # role lives on update_oidc (it also reconciles OIDC role); reads the row
            # update_credentials just wrote, so a combined edit returns the final state.
            updated = store.update_oidc(user_id, role=body.role)
    except DuplicateUser as exc:
        raise HTTPException(status_code=409, detail="email already in use") from exc
    except InvalidUserValue as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        # The row was deleted between the get_by_id above and the update (a concurrent
        # DELETE); the store raises KeyError. Report it as 404, not a 500.
        raise HTTPException(status_code=404, detail="user not found") from exc
    return UserSummaryOut(
        userId=updated.user_id,
        username=updated.username,
        role=updated.role,
        email=updated.email,
        isEnvAdmin=False,  # the env-admin is refused above, so this row never is one
    )


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, admin: Principal = Depends(require_admin)) -> None:
    # An admin can't delete their own account (avoids locking yourself out mid-session).
    if user_id == admin.user_id:
        raise HTTPException(
            status_code=400, detail="you cannot remove your own account"
        )
    store = get_user_store()
    target = store.get_by_id(user_id)
    # Scope deletion to the admin's own household; 404 (not 403) avoids leaking
    # whether a user id exists in another household.
    if target is None or target.household != admin.household:
        raise HTTPException(status_code=404, detail="user not found")
    env_admin = store.get_env_admin()
    if env_admin is not None and env_admin.user_id == user_id:
        # The env-managed admin is reconciled from API_AUTH_ADMIN_* on every boot, so
        # deleting it just resurrects on restart — refuse rather than mislead.
        raise HTTPException(
            status_code=409, detail="the env-managed admin cannot be removed"
        )
    if target.oidc_sub is not None:
        # An OIDC account is governed by Authentik, not deleted here (docs/auth-oidc.md
        # §8). Local deletion would not revoke it: a still-valid Authentik access token
        # re-provisions the account on the next request (group gate permitting), so a
        # deleted admin could silently re-appear as admin. Revoke by removing the user
        # from the Tenir group in Authentik instead — the group gate denies them on
        # their next login.
        raise HTTPException(
            status_code=409,
            detail=(
                "an OIDC account cannot be removed here; remove the user from the Tenir "
                "group in Authentik to revoke their access"
            ),
        )
    store.delete(user_id)
    # Auth is checked at the WS handshake only, so a live capture socket keeps
    # recording into the household after its account is gone. Close them here so
    # "removed" means removed on every surface (XERK-236). Each session is
    # finalized normally, so nothing already captured is lost.
    doomed = [s for s in registry.active() if s.user_id == user_id]
    for session in doomed:
        registry.unregister(session)
    # Concurrently (XERK-1496): one close against a hung model takes ~30 s, so one
    # after another a user with several sessions held this request — and, since
    # uvicorn drains in-flight requests on SIGTERM, the pod's shutdown — past the
    # SIGKILL. A failing revoke is logged and doesn't skip the others.
    results = await asyncio.gather(
        *(s.revoke("account deleted") for s in doomed), return_exceptions=True
    )
    for session, result in zip(doomed, results, strict=True):
        if isinstance(result, Exception):
            log.error("session %s revoke failed", session.session_id, exc_info=result)
    # Meeting-capture sockets (XERK-2505) aren't registry sessions: stop them too. Imported
    # here to keep the auth package free of the screen package at import time.
    from api.screen.ws import revoke_user

    revoke_user(user_id)
