"""Postgres-backed user store (master plan §7, Phase 6).

The durable ``UserStore``: household members in Postgres (schema in
``schema.sql``), the multi-process swap for ``InMemoryUserStore`` behind the same
Protocol. Selected when ``API_PERSISTENCE_BACKEND=postgres``. Mirrors
``SqlConversationStore``: a lazy ``psycopg_pool`` pool opened on first use, so the
api boots (and the factory can select this backend) without a live database.

Because ``schema.sql`` only runs on a fresh data volume and there is no migration
framework, the store runs idempotent DDL once on pool open so an already-initialized
database picks up the ``is_env_admin`` column. The SQL methods need a running
Postgres, so they are excluded from coverage; the store contract they implement is
covered by ``InMemoryUserStore`` tests, and the schema is exercised by the compose
stack.
"""

from __future__ import annotations

import logging
import threading
import uuid

from api.auth.tokens import Role, hash_password, verify_password
from api.auth.users import DuplicateUser, User
from api.persistence.postgres import PoolOpener, apply_boot_schema

log = logging.getLogger("api.auth.sql_users")


def _is_uuid(user_id: str) -> bool:
    """Whether ``user_id`` is a canonical (lowercase, hyphenated) UUID string.

    ``users.id`` is a UUID column and Postgres rejects a non-UUID literal with
    InvalidTextRepresentation (22P02) rather than matching nothing, so an id from a
    URL like ``/auth/users/x`` is screened first: it is "no such user" (404), as on
    ``InMemoryUserStore``, not a 500 (XERK-1532). Only the canonical form passes —
    Postgres would also match ``{...}``/uppercase/unhyphenated spellings of a real id,
    slipping them past the router's string-equality guards (self-delete, env admin)."""
    try:
        return str(uuid.UUID(user_id)) == user_id
    except (ValueError, TypeError, AttributeError):
        return False

# psycopg3 executes one statement per call (extended protocol), so keep these
# separate rather than one multi-statement string.
_ENSURE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        household      TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
        username       TEXT NOT NULL UNIQUE,
        role           TEXT NOT NULL DEFAULT 'member',
        password_hash  TEXT,
        oidc_sub       TEXT,
        email          TEXT,
        is_env_admin   BOOLEAN NOT NULL DEFAULT false,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_env_admin BOOLEAN NOT NULL DEFAULT false",
    # OIDC identity (XERK-650, T4): additive on databases created before it, and
    # password_hash was NOT NULL before OIDC-only rows existed.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_sub TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT",
    "ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS users_one_env_admin_idx ON users (is_env_admin) WHERE is_env_admin",
    "CREATE UNIQUE INDEX IF NOT EXISTS users_oidc_sub_idx ON users (oidc_sub) WHERE oidc_sub IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (lower(email)) WHERE email IS NOT NULL",
    # Usernames are looked up case-insensitively, so they must be unique that way too:
    # the column's own UNIQUE let "alice" and "ALICE" coexist, and login then matched
    # whichever row Postgres returned first (XERK-1535). A database that already holds
    # case-variant duplicates can't take the index; creating it unconditionally would
    # abort boot (SchemaApplyError), so it is skipped there and _ensure_schema logs the
    # rows to rename. The next boot after the rename creates it.
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM users GROUP BY lower(username) HAVING count(*) > 1) THEN
            CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_idx ON users (lower(username));
        END IF;
    END
    $$
    """,
)

# Case-variant duplicate usernames, which block users_username_lower_idx.
_DUPLICATE_USERNAMES = (
    "SELECT array_agg(username ORDER BY created_at, id) FROM users"
    " GROUP BY lower(username) HAVING count(*) > 1"
)

# users.household REFERENCES households(id) and schema.sql seeds only 'default', so a
# write placing a user in any other household creates that row first, in the same
# transaction (XERK-1508). Without it a non-default API_AUTH_ADMIN_HOUSEHOLD, or any
# member/OIDC user created or moved into a new household, failed users_household_fkey.
_ENSURE_HOUSEHOLD = "INSERT INTO households (id) VALUES (%s) ON CONFLICT (id) DO NOTHING"

# Every read selects the same column set so a row maps cleanly to ``User``.
_USER_COLUMNS = "id, household, username, role, password_hash, oidc_sub, email"


class SqlUserStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None
        # Serializes pool open + schema apply so concurrent first callers don't
        # each open a pool and race the DDL (XERK-1409).
        self._pool_lock = threading.Lock()
        self._opener = PoolOpener(dsn, "users")

    def _ensure_pool(self):  # pragma: no cover - requires psycopg + a live database
        if self._pool is not None:
            return self._pool
        with self._pool_lock:
            if self._pool is not None:
                return self._pool
            # Cache the pool only once its schema applied, so a failed apply is
            # retried on next use rather than silently skipped forever (XERK-1409).
            self._pool = self._opener.open(self._ensure_schema)
        return self._pool

    @staticmethod
    def _ensure_schema(pool) -> None:  # pragma: no cover - requires a live database
        # schema.sql first, under the same cross-process lock as the conversation
        # store's apply: the users DDL references households, which on an empty
        # database doesn't exist until schema.sql ran (XERK-1430).
        apply_boot_schema(pool, _ENSURE_SCHEMA)
        with pool.connection() as conn:
            dupes = [row[0] for row in conn.execute(_DUPLICATE_USERNAMES).fetchall()]
        if dupes:
            log.error(
                "users holds case-variant duplicate usernames %s; case-insensitive username"
                " uniqueness is NOT enforced until all but one of each group is renamed or"
                " deleted (login resolves to the exact-case match, else the oldest row)",
                dupes,
            )

    @staticmethod
    def _row_to_user(row) -> User:  # pragma: no cover - requires a live database
        return User(
            user_id=str(row["id"]),
            username=row["username"],
            household=row["household"],
            role="admin" if row["role"] == "admin" else "member",
            password_hash=row["password_hash"],
            oidc_sub=row.get("oidc_sub"),
            email=row.get("email"),
        )

    def create(  # pragma: no cover - requires a live database
        self,
        username: str,
        password: str,
        *,
        household: str,
        role: Role = "member",
        is_env_admin: bool = False,
        email: str | None = None,
    ) -> User:
        from psycopg.errors import UniqueViolation
        from psycopg.rows import dict_row

        try:
            with self._ensure_pool().connection() as conn:
                conn.execute(_ENSURE_HOUSEHOLD, (household,))
                # Cursor-scoped row factory: psycopg's pool doesn't reset
                # row_factory, so mutating the pooled connection would poison the
                # next borrower with dict rows.
                row = conn.cursor(row_factory=dict_row).execute(
                    f"""
                    INSERT INTO users (household, username, role, password_hash, is_env_admin, email)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING {_USER_COLUMNS}
                    """,
                    (household, username, role, hash_password(password), is_env_admin, email),
                ).fetchone()
        except UniqueViolation as exc:
            raise DuplicateUser(username) from exc
        return self._row_to_user(row)

    def create_oidc(  # pragma: no cover - requires a live database
        self,
        *,
        oidc_sub: str,
        email: str | None,
        username: str,
        household: str,
        role: Role,
    ) -> User:
        from psycopg.errors import UniqueViolation
        from psycopg.rows import dict_row

        try:
            with self._ensure_pool().connection() as conn:
                conn.execute(_ENSURE_HOUSEHOLD, (household,))
                row = conn.cursor(row_factory=dict_row).execute(
                    f"""
                    INSERT INTO users (household, username, role, password_hash, oidc_sub, email)
                    VALUES (%s, %s, %s, NULL, %s, %s)
                    RETURNING {_USER_COLUMNS}
                    """,
                    (household, username, role, oidc_sub, email),
                ).fetchone()
        except UniqueViolation as exc:
            raise DuplicateUser(username) from exc
        return self._row_to_user(row)

    def get_by_username(self, username: str) -> User | None:  # pragma: no cover
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            row = conn.cursor(row_factory=dict_row).execute(
                # Unique once users_username_lower_idx exists; on a database whose
                # legacy duplicates blocked it, prefer the exact-case row, then the
                # oldest, so login is deterministic (XERK-1535).
                f"SELECT {_USER_COLUMNS} FROM users WHERE lower(username) = lower(%s)"
                " ORDER BY (username = %s) DESC, created_at, id LIMIT 1",
                (username.strip(), username.strip()),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_id(self, user_id: str) -> User | None:  # pragma: no cover
        from psycopg.rows import dict_row

        if not _is_uuid(user_id):
            return None
        with self._ensure_pool().connection() as conn:
            row = conn.cursor(row_factory=dict_row).execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_oidc_sub(self, oidc_sub: str) -> User | None:  # pragma: no cover
        from psycopg.rows import dict_row

        if not oidc_sub:
            return None
        with self._ensure_pool().connection() as conn:
            row = conn.cursor(row_factory=dict_row).execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE oidc_sub = %s",
                (oidc_sub,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_email(self, email: str) -> User | None:  # pragma: no cover
        from psycopg.rows import dict_row

        key = (email or "").strip()
        if not key:
            return None
        with self._ensure_pool().connection() as conn:
            row = conn.cursor(row_factory=dict_row).execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE lower(email) = lower(%s)",
                (key,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_env_admin(self) -> User | None:  # pragma: no cover
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            row = conn.cursor(row_factory=dict_row).execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE is_env_admin",
            ).fetchone()
        return self._row_to_user(row) if row else None

    def list_by_household(self, household: str) -> list[User]:  # pragma: no cover
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            rows = conn.cursor(row_factory=dict_row).execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE household = %s ORDER BY lower(username)",
                (household,),
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def delete(self, user_id: str) -> None:  # pragma: no cover
        if not _is_uuid(user_id):
            raise KeyError(user_id)
        with self._ensure_pool().connection() as conn:
            cur = conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
            if cur.rowcount == 0:
                raise KeyError(user_id)

    def authenticate(self, username: str, password: str) -> User | None:  # pragma: no cover
        user = self.get_by_username(username)
        # An OIDC-only row has no password_hash and can never authenticate locally.
        if user is None or not user.password_hash:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    def update_credentials(  # pragma: no cover - requires a live database
        self,
        user_id: str,
        *,
        username: str | None = None,
        password: str | None = None,
        household: str | None = None,
        email: str | None = None,
    ) -> User:
        from psycopg.errors import UniqueViolation
        from psycopg.rows import dict_row

        if not _is_uuid(user_id):
            raise KeyError(user_id)
        sets: list[str] = []
        params: list[object] = []
        if username is not None:
            sets.append("username = %s")
            params.append(username)
        if password is not None:
            sets.append("password_hash = %s")
            params.append(hash_password(password))
        if household is not None:
            sets.append("household = %s")
            params.append(household)
        if email is not None:
            sets.append("email = %s")
            params.append(email)
        if not sets:
            got = self.get_by_id(user_id)
            if got is None:
                raise KeyError(user_id)
            return got
        params.append(user_id)
        try:
            with self._ensure_pool().connection() as conn:
                if household is not None:
                    conn.execute(_ENSURE_HOUSEHOLD, (household,))
                row = conn.cursor(row_factory=dict_row).execute(
                    f"UPDATE users SET {', '.join(sets)} WHERE id = %s"
                    f" RETURNING {_USER_COLUMNS}",
                    tuple(params),
                ).fetchone()
                if row is None:
                    # Raised inside the block so the transaction rolls back and a
                    # missing user leaves no households row behind.
                    raise KeyError(user_id)
        except UniqueViolation as exc:
            raise DuplicateUser(username or "") from exc
        return self._row_to_user(row)

    def update_oidc(  # pragma: no cover - requires a live database
        self,
        user_id: str,
        *,
        oidc_sub: str | None = None,
        username: str | None = None,
        role: Role | None = None,
    ) -> User:
        from psycopg.errors import UniqueViolation
        from psycopg.rows import dict_row

        if not _is_uuid(user_id):
            raise KeyError(user_id)
        sets: list[str] = []
        params: list[object] = []
        if oidc_sub is not None:
            sets.append("oidc_sub = %s")
            params.append(oidc_sub)
        if username is not None:
            sets.append("username = %s")
            params.append(username)
        if role is not None:
            sets.append("role = %s")
            params.append(role)
        if not sets:
            got = self.get_by_id(user_id)
            if got is None:
                raise KeyError(user_id)
            return got
        params.append(user_id)
        try:
            with self._ensure_pool().connection() as conn:
                row = conn.cursor(row_factory=dict_row).execute(
                    f"UPDATE users SET {', '.join(sets)} WHERE id = %s"
                    f" RETURNING {_USER_COLUMNS}",
                    tuple(params),
                ).fetchone()
        except UniqueViolation as exc:
            raise DuplicateUser(username or oidc_sub or "") from exc
        if row is None:
            raise KeyError(user_id)
        return self._row_to_user(row)
