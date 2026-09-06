"""User & household store (master plan §7, decision #6, Phase 6).

The accounts behind the auth service: each user belongs to exactly one household
(the team/sharing boundary), with a role that gates admin-only controls (the
master capture toggle, retention). Phase 6 ships an in-memory implementation
behind the ``UserStore`` Protocol; a Postgres/OIDC swap (Authelia/Keycloak, §7)
drops in behind the same seam later.

A process-wide singleton (``get_user_store``) is shared by the login endpoint and
the request dependency. An optional bootstrap admin (``API_AUTH_ADMIN_*``) is
created on first access so a fresh instance has someone who can log in.

OIDC identity (XERK-650, T4). A row may be **local-only** (password, no
``oidc_sub``), **OIDC-only** (``oidc_sub``, no password) or **linked** (both). A
validated Authentik token is resolved to a local row by ``resolve_oidc_principal``
in the fixed order of docs/auth-oidc.md §5 — by ``oidc_sub``, else by *verified*
email (the hard security guard: an unverified email is never a link key), else
JIT-provision — so the env-admin (and any pre-created local member) adopts their
Authentik identity while keeping their id, role, and owned recordings.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Protocol

from api.auth.tokens import Principal, Role, hash_password, verify_password
from api.config import settings

log = logging.getLogger("api.auth.users")


@dataclass
class User:
    """A household member who can authenticate.

    ``password_hash`` is ``None`` for an OIDC-only row (no local password); ``email``
    and ``oidc_sub`` are set on OIDC/linked rows (both are unique when present, email
    case-insensitively) and ``None`` on a plain local row.
    """

    user_id: str
    username: str
    household: str
    role: Role
    password_hash: str | None = None
    oidc_sub: str | None = None
    email: str | None = None


class UserStore(Protocol):
    def create(
        self,
        username: str,
        password: str,
        *,
        household: str,
        role: Role = "member",
        is_env_admin: bool = False,
        email: str | None = None,
    ) -> User: ...
    def get_by_username(self, username: str) -> User | None: ...
    def get_by_id(self, user_id: str) -> User | None: ...
    def get_by_oidc_sub(self, oidc_sub: str) -> User | None: ...
    def get_by_email(self, email: str) -> User | None: ...
    def get_env_admin(self) -> User | None: ...
    def list_by_household(self, household: str) -> list[User]: ...
    def delete(self, user_id: str) -> None: ...
    def authenticate(self, username: str, password: str) -> User | None: ...
    def update_credentials(
        self,
        user_id: str,
        *,
        username: str | None = None,
        password: str | None = None,
        household: str | None = None,
        email: str | None = None,
    ) -> User: ...
    def create_oidc(
        self,
        *,
        oidc_sub: str,
        email: str | None,
        username: str,
        household: str,
        role: Role,
    ) -> User: ...
    def update_oidc(
        self,
        user_id: str,
        *,
        oidc_sub: str | None = None,
        username: str | None = None,
        role: Role | None = None,
    ) -> User: ...


class DuplicateUser(Exception):
    """Raised when creating or renaming a user to a username/email already taken."""


def _norm(value: str | None) -> str | None:
    """Lower-case a non-empty string for case-insensitive indexing; else ``None``."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped.lower() if stripped else None


class InMemoryUserStore:
    """Thread-safe in-memory ``UserStore`` (default backend).

    Indexed by username (login), user_id (the stable identity), ``oidc_sub`` and
    ``email`` (both used to resolve an OIDC token to its local row). A
    username/password/link change keeps the same row. Usernames are unique and
    case-insensitive; emails are unique case-insensitively when set. The single
    env-managed admin is tracked by its user_id.
    """

    def __init__(self) -> None:
        self._by_username: dict[str, User] = {}
        self._by_id: dict[str, User] = {}
        self._by_oidc_sub: dict[str, User] = {}
        self._by_email: dict[str, User] = {}
        self._env_admin_id: str | None = None
        self._lock = threading.Lock()

    # --- index maintenance (called under the lock) ---------------------------

    def _index(self, user: User) -> None:
        self._by_username[user.username.strip().lower()] = user
        self._by_id[user.user_id] = user
        if user.oidc_sub:
            self._by_oidc_sub[user.oidc_sub] = user
        email_key = _norm(user.email)
        if email_key:
            self._by_email[email_key] = user

    def _deindex(self, user: User) -> None:
        self._by_username.pop(user.username.strip().lower(), None)
        if user.oidc_sub:
            self._by_oidc_sub.pop(user.oidc_sub, None)
        email_key = _norm(user.email)
        if email_key:
            self._by_email.pop(email_key, None)

    def create(
        self,
        username: str,
        password: str,
        *,
        household: str,
        role: Role = "member",
        is_env_admin: bool = False,
        email: str | None = None,
    ) -> User:
        key = username.strip().lower()
        email_key = _norm(email)
        with self._lock:
            if key in self._by_username:
                raise DuplicateUser(username)
            if email_key and email_key in self._by_email:
                raise DuplicateUser(email or "")
            user = User(
                user_id=str(uuid.uuid4()),
                username=username,
                household=household,
                role=role,
                password_hash=hash_password(password),
                email=email,
            )
            self._index(user)
            if is_env_admin:
                self._env_admin_id = user.user_id
            return user

    def create_oidc(
        self,
        *,
        oidc_sub: str,
        email: str | None,
        username: str,
        household: str,
        role: Role,
    ) -> User:
        key = username.strip().lower()
        email_key = _norm(email)
        with self._lock:
            if key in self._by_username:
                raise DuplicateUser(username)
            if oidc_sub in self._by_oidc_sub:
                raise DuplicateUser(oidc_sub)
            if email_key and email_key in self._by_email:
                raise DuplicateUser(email or "")
            user = User(
                user_id=str(uuid.uuid4()),
                username=username,
                household=household,
                role=role,
                password_hash=None,  # OIDC-only: no local password
                oidc_sub=oidc_sub,
                email=email,
            )
            self._index(user)
            return user

    def get_by_username(self, username: str) -> User | None:
        with self._lock:
            return self._by_username.get(username.strip().lower())

    def get_by_id(self, user_id: str) -> User | None:
        with self._lock:
            return self._by_id.get(user_id)

    def get_by_oidc_sub(self, oidc_sub: str) -> User | None:
        with self._lock:
            return self._by_oidc_sub.get(oidc_sub) if oidc_sub else None

    def get_by_email(self, email: str) -> User | None:
        key = _norm(email)
        with self._lock:
            return self._by_email.get(key) if key else None

    def get_env_admin(self) -> User | None:
        with self._lock:
            return self._by_id.get(self._env_admin_id) if self._env_admin_id else None

    def list_by_household(self, household: str) -> list[User]:
        with self._lock:
            return sorted(
                (u for u in self._by_id.values() if u.household == household),
                key=lambda u: u.username.strip().lower(),
            )

    def delete(self, user_id: str) -> None:
        with self._lock:
            user = self._by_id.pop(user_id, None)
            if user is None:
                raise KeyError(user_id)
            self._deindex(user)
            if self._env_admin_id == user_id:
                self._env_admin_id = None

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.get_by_username(username)
        # An OIDC-only row has no password_hash and can never authenticate locally.
        if user is None or not user.password_hash:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    def update_credentials(
        self,
        user_id: str,
        *,
        username: str | None = None,
        password: str | None = None,
        household: str | None = None,
        email: str | None = None,
    ) -> User:
        with self._lock:
            user = self._by_id.get(user_id)
            if user is None:
                raise KeyError(user_id)
            new_username = user.username if username is None else username
            new_key = new_username.strip().lower()
            if new_key != user.username.strip().lower() and new_key in self._by_username:
                raise DuplicateUser(new_username)
            new_email = user.email if email is None else email
            new_email_key = _norm(new_email)
            if new_email_key and new_email_key != _norm(user.email):
                owner = self._by_email.get(new_email_key)
                if owner is not None and owner.user_id != user_id:
                    raise DuplicateUser(email or "")
            updated = replace(
                user,
                username=new_username,
                household=user.household if household is None else household,
                password_hash=(
                    user.password_hash if password is None else hash_password(password)
                ),
                email=new_email,
            )
            self._deindex(user)
            self._index(updated)
            return updated

    def update_oidc(
        self,
        user_id: str,
        *,
        oidc_sub: str | None = None,
        username: str | None = None,
        role: Role | None = None,
    ) -> User:
        """Link (set ``oidc_sub``) and/or refresh username/role on an existing row.

        Used by ``resolve_oidc_principal`` to link a local row to its Authentik
        identity (step 2) and to refresh username/role from the token on every login
        (steps 1 & 2). Identity (``user_id``) never changes. Raises ``DuplicateUser``
        if the new username or ``oidc_sub`` is already taken by another row.
        """
        with self._lock:
            user = self._by_id.get(user_id)
            if user is None:
                raise KeyError(user_id)
            new_username = user.username if username is None else username
            new_key = new_username.strip().lower()
            if new_key != user.username.strip().lower() and new_key in self._by_username:
                raise DuplicateUser(new_username)
            new_sub = user.oidc_sub if oidc_sub is None else oidc_sub
            if new_sub and new_sub != user.oidc_sub:
                owner = self._by_oidc_sub.get(new_sub)
                if owner is not None and owner.user_id != user_id:
                    raise DuplicateUser(new_sub)
            updated = replace(
                user,
                username=new_username,
                role=user.role if role is None else role,
                oidc_sub=new_sub,
            )
            self._deindex(user)
            self._index(updated)
            return updated


_store: UserStore | None = None
_store_lock = threading.Lock()


def _build_user_store() -> UserStore:
    if settings.persistence_backend == "postgres":
        from api.auth.sql_users import SqlUserStore

        return SqlUserStore(settings.database_url)
    return InMemoryUserStore()


def reconcile_admin(store: UserStore) -> None:
    """Create-or-update the env-managed admin from ``API_AUTH_ADMIN_*``.

    Runs on first store access when both admin vars are set. The admin is anchored
    on its stable ``user_id`` (via the ``is_env_admin`` marker), so changing the env
    username/password updates that row in place — the id (and anything referencing
    it, plus the untouched voiceprints) is preserved. Env is the source of truth on
    every boot; unsetting the vars is a no-op.

    ``API_AUTH_ADMIN_EMAIL`` (T4) is reconciled onto the row when set, so the row is
    the verified-email link target when the operator first logs in through Authentik
    (docs/auth-oidc.md §6). Left empty, the existing email is untouched.
    """
    if not (settings.auth_admin_username and settings.auth_admin_password):
        return
    email = settings.auth_admin_email.strip() or None
    existing = store.get_env_admin()
    if existing is None:
        try:
            store.create(
                settings.auth_admin_username,
                settings.auth_admin_password,
                household=settings.auth_admin_household,
                role="admin",
                is_env_admin=True,
                email=email,
            )
        except DuplicateUser:
            # The email (or username) collides with another row — seed the admin
            # without the email rather than fail boot; the operator can resolve the
            # clash and reboot.
            log.warning(
                "could not seed env admin with email %r (already taken); seeding "
                "without an email link key",
                email,
            )
            store.create(
                settings.auth_admin_username,
                settings.auth_admin_password,
                household=settings.auth_admin_household,
                role="admin",
                is_env_admin=True,
            )
        return
    try:
        store.update_credentials(
            existing.user_id,
            username=settings.auth_admin_username,
            password=settings.auth_admin_password,
            household=settings.auth_admin_household,
            email=email,
        )
    except DuplicateUser:
        log.warning(
            "env admin username %r or email %r is already taken by another user; "
            "keeping the current admin username/email",
            settings.auth_admin_username,
            email,
        )


def _principal_for(user: User, token: Principal) -> Principal:
    """Build the request ``Principal`` from a resolved local row + the OIDC token.

    ``user_id``/``household``/``role``/``username`` come from the **local row** (the
    stable identity everything downstream keys on, docs/auth-oidc.md §8); ``sub``,
    ``email``, ``email_verified`` and ``groups`` are carried through from the token
    for audit.
    """
    return Principal(
        user_id=user.user_id,
        household=user.household,
        role=user.role,
        username=user.username,
        sub=token.sub,
        email=token.email,
        email_verified=token.email_verified,
        groups=token.groups,
    )


def resolve_oidc_principal(token: Principal, store: UserStore | None = None) -> Principal:
    """Resolve a validated OIDC token to a local user, per docs/auth-oidc.md §5.

    Fixed order, first hit wins: (1) ``oidc_sub`` match → already-linked row;
    (2) *verified*-email match → link the token's ``sub`` onto that existing local
    row (keeping its id/role/recordings); (3) JIT-provision a new OIDC-only row.

    Security guard (non-negotiable): step 2 links **only** when the token's
    ``email_verified`` is ``true``. An unverified email is never a link key — that
    would let an Authentik account claim a local account and all its recordings — so
    an unverified email falls through to a fresh JIT row and is not stored (it must
    not become a future link key).

    Role reconciliation: role comes from the token's groups on every login for JIT
    and linked users; the env-admin is always ``admin`` regardless of group
    membership (§6), and its env-managed username is left untouched.
    """
    store = store or get_user_store()
    sub = token.sub or ""
    email = token.email

    user = store.get_by_oidc_sub(sub)
    if user is None and token.email_verified and email:
        match = store.get_by_email(email)
        if match is not None:
            # Link the token's sub onto the existing (local, or sub-rotated) row.
            user = store.update_oidc(match.user_id, oidc_sub=sub)

    if user is None:
        # JIT-provision. Only a verified email is stored (it is the link key); an
        # unverified email is dropped so it can never become one.
        return _principal_for(
            _jit_create(store, token, email if token.email_verified else None),
            token,
        )

    return _principal_for(_refresh_login(store, user, token), token)


def _refresh_login(store: UserStore, user: User, token: Principal) -> User:
    """Refresh username/role from the token on an existing (by-sub or just-linked) row.

    The env-admin is the sole exception: it stays ``admin`` regardless of groups and
    keeps its env-managed username (docs/auth-oidc.md §6/§7). For everyone else a
    username collision with another row is non-fatal — keep the current username
    (login must not fail because two IdP users picked the same ``preferred_username``).
    """
    env_admin = store.get_env_admin()
    is_env_admin = env_admin is not None and env_admin.user_id == user.user_id
    role: Role = "admin" if is_env_admin else token.role
    username = user.username if is_env_admin else (token.username.strip() or user.username)
    if username == user.username and role == user.role:
        return user
    try:
        return store.update_oidc(user.user_id, username=username, role=role)
    except DuplicateUser:
        # Username taken by another row — keep ours, still apply the role change.
        return store.update_oidc(user.user_id, username=user.username, role=role)


def _jit_create(store: UserStore, token: Principal, email: str | None) -> User:
    """JIT-provision a new OIDC-only row, picking a free username.

    Prefer the token's ``preferred_username``; fall back to the email local-part,
    then the ``sub`` (globally unique), so a collision with an existing local
    username never blocks provisioning.
    """
    sub = token.sub or ""
    household = token.household
    candidates = [
        token.username.strip(),
        (email.split("@", 1)[0] if email else ""),
        sub,
    ]
    seen: set[str] = set()
    last_exc: DuplicateUser | None = None
    for candidate in candidates:
        if not candidate or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        try:
            return store.create_oidc(
                oidc_sub=sub,
                email=email,
                username=candidate,
                household=household,
                role=token.role,
            )
        except DuplicateUser as exc:
            last_exc = exc
            continue
    # Every candidate collided (only reachable if the sub itself is somehow taken).
    raise last_exc or DuplicateUser(sub)


def get_user_store() -> UserStore:
    """The process-wide user store, with the env admin reconciled on first use."""
    global _store
    with _store_lock:
        if _store is None:
            _store = _build_user_store()
            reconcile_admin(_store)
        return _store


def reset_user_store() -> None:
    """Drop the singleton so tests start from a clean store (re-seeds on next use)."""
    global _store
    with _store_lock:
        _store = None
