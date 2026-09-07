"""Auth REST API (master plan §7, Phase 6).

The login surface for the companion/admin pages: exchange a username + password
for a bearer token, and read back the current principal. Admins can also create
additional household members.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import registry
from api.auth.deps import current_principal, require_admin
from api.auth.tokens import Principal, Role, issue_token
from api.auth.users import DuplicateUser, get_user_store
from api.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


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


class UserSummaryOut(BaseModel):
    userId: str
    username: str
    role: str
    # The env-managed bootstrap admin (API_AUTH_ADMIN_*) is reconciled from env on
    # every boot, so removing it is pointless — the UI greys out its delete control.
    isEnvAdmin: bool = False


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn) -> TokenOut:
    user = get_user_store().authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid username or password")
    principal = Principal(
        user_id=user.user_id, username=user.username, household=user.household, role=user.role
    )
    token = issue_token(
        principal, secret=settings.auth_secret, ttl_seconds=settings.auth_token_ttl_seconds
    )
    return TokenOut(
        token=token,
        expiresIn=settings.auth_token_ttl_seconds,
        userId=user.user_id,
        username=user.username,
        household=user.household,
        role=user.role,
    )


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
            isEnvAdmin=u.user_id == env_admin_id,
        )
        for u in store.list_by_household(admin.household)
    ]


@router.post("/users", response_model=PrincipalOut, status_code=201)
def create_user(body: CreateUserIn, admin: Principal = Depends(require_admin)) -> PrincipalOut:
    # New members join the admin's household — the team boundary (decision #6).
    try:
        user = get_user_store().create(
            body.username, body.password, household=admin.household, role=body.role
        )
    except DuplicateUser as exc:
        raise HTTPException(status_code=409, detail="username already taken") from exc
    return PrincipalOut.of(
        Principal(
            user_id=user.user_id, username=user.username, household=user.household, role=user.role
        )
    )


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, admin: Principal = Depends(require_admin)) -> None:
    # An admin can't delete their own account (avoids locking yourself out mid-session).
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="you cannot remove your own account")
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
        raise HTTPException(status_code=409, detail="the env-managed admin cannot be removed")
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
    for session in registry.active():
        if session.user_id == user_id:
            registry.unregister(session)
            await session.revoke("account deleted")
