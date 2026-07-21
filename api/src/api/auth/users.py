"""User & household store (master plan §7, decision #6, Phase 6).

The accounts behind the auth service: each user belongs to exactly one household
(the team/sharing boundary), with a role that gates admin-only controls (the
master capture toggle, retention). Phase 6 ships an in-memory implementation
behind the ``UserStore`` Protocol; a Postgres/OIDC swap (Authelia/Keycloak, §7)
drops in behind the same seam later.

A process-wide singleton (``get_user_store``) is shared by the login endpoint and
the request dependency. An optional bootstrap admin (``API_AUTH_ADMIN_*``) is
created on first access so a fresh instance has someone who can log in.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Protocol

from api.auth.tokens import Role, hash_password, verify_password
from api.config import settings

log = logging.getLogger("api.auth.users")


@dataclass
class User:
    """A household member who can authenticate."""

    user_id: str
    username: str
    household: str
    role: Role
    password_hash: str


class UserStore(Protocol):
    def create(
        self,
        username: str,
        password: str,
        *,
        household: str,
        role: Role = "member",
        is_env_admin: bool = False,
    ) -> User: ...
    def get_by_username(self, username: str) -> User | None: ...
    def get_by_id(self, user_id: str) -> User | None: ...
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
    ) -> User: ...


class DuplicateUser(Exception):
    """Raised when creating or renaming a user to a username already taken."""


class InMemoryUserStore:
    """Thread-safe in-memory ``UserStore`` (default backend).

    Indexed by both username (login) and user_id (the stable identity), so a
    username/password change keeps the same row. Usernames are unique and
    case-insensitive. The single env-managed admin is tracked by its user_id.
    """

    def __init__(self) -> None:
        self._by_username: dict[str, User] = {}
        self._by_id: dict[str, User] = {}
        self._env_admin_id: str | None = None
        self._lock = threading.Lock()

    def create(
        self,
        username: str,
        password: str,
        *,
        household: str,
        role: Role = "member",
        is_env_admin: bool = False,
    ) -> User:
        key = username.strip().lower()
        with self._lock:
            if key in self._by_username:
                raise DuplicateUser(username)
            user = User(
                user_id=str(uuid.uuid4()),
                username=username,
                household=household,
                role=role,
                password_hash=hash_password(password),
            )
            self._by_username[key] = user
            self._by_id[user.user_id] = user
            if is_env_admin:
                self._env_admin_id = user.user_id
            return user

    def get_by_username(self, username: str) -> User | None:
        with self._lock:
            return self._by_username.get(username.strip().lower())

    def get_by_id(self, user_id: str) -> User | None:
        with self._lock:
            return self._by_id.get(user_id)

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
            self._by_username.pop(user.username.strip().lower(), None)
            if self._env_admin_id == user_id:
                self._env_admin_id = None

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.get_by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            return None
        return user

    def update_credentials(
        self,
        user_id: str,
        *,
        username: str | None = None,
        password: str | None = None,
        household: str | None = None,
    ) -> User:
        with self._lock:
            user = self._by_id.get(user_id)
            if user is None:
                raise KeyError(user_id)
            new_username = user.username if username is None else username
            old_key = user.username.strip().lower()
            new_key = new_username.strip().lower()
            if new_key != old_key and new_key in self._by_username:
                raise DuplicateUser(new_username)
            updated = User(
                user_id=user.user_id,  # identity never changes
                username=new_username,
                household=user.household if household is None else household,
                role=user.role,
                password_hash=(
                    user.password_hash if password is None else hash_password(password)
                ),
            )
            if new_key != old_key:
                del self._by_username[old_key]
            self._by_username[new_key] = updated
            self._by_id[updated.user_id] = updated
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
    """
    if not (settings.auth_admin_username and settings.auth_admin_password):
        return
    existing = store.get_env_admin()
    if existing is None:
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
        )
    except DuplicateUser:
        log.warning(
            "env admin username %r is already taken by another user; keeping the "
            "current admin username",
            settings.auth_admin_username,
        )


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
