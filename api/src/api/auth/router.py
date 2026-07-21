"""Auth REST API (master plan §7, Phase 6).

The login surface for the companion/admin pages: exchange a username + password
for a bearer token, and read back the current principal. Admins can also create
additional household members.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

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
def delete_user(user_id: str, admin: Principal = Depends(require_admin)) -> None:
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
    store.delete(user_id)
