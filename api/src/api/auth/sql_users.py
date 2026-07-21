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

from api.auth.tokens import Role, hash_password, verify_password
from api.auth.users import DuplicateUser, User

log = logging.getLogger("api.auth.sql_users")

# psycopg3 executes one statement per call (extended protocol), so keep these
# separate rather than one multi-statement string.
_ENSURE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        household      TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
        username       TEXT NOT NULL UNIQUE,
        role           TEXT NOT NULL DEFAULT 'member',
        password_hash  TEXT NOT NULL,
        is_env_admin   BOOLEAN NOT NULL DEFAULT false,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_env_admin BOOLEAN NOT NULL DEFAULT false",
    "CREATE UNIQUE INDEX IF NOT EXISTS users_one_env_admin_idx ON users (is_env_admin) WHERE is_env_admin",
)


class SqlUserStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None

    def _ensure_pool(self):  # pragma: no cover - requires psycopg + a live database
        if self._pool is None:
            from psycopg_pool import ConnectionPool

            log.info("opening Postgres connection pool (users)")
            self._pool = ConnectionPool(self._dsn, open=True)
            with self._pool.connection() as conn:
                for stmt in _ENSURE_SCHEMA:
                    conn.execute(stmt)
        return self._pool

    @staticmethod
    def _row_to_user(row) -> User:  # pragma: no cover - requires a live database
        return User(
            user_id=str(row["id"]),
            username=row["username"],
            household=row["household"],
            role="admin" if row["role"] == "admin" else "member",
            password_hash=row["password_hash"],
        )

    def create(  # pragma: no cover - requires a live database
        self,
        username: str,
        password: str,
        *,
        household: str,
        role: Role = "member",
        is_env_admin: bool = False,
    ) -> User:
        from psycopg.errors import UniqueViolation
        from psycopg.rows import dict_row

        try:
            with self._ensure_pool().connection() as conn:
                # Cursor-scoped row factory: psycopg's pool doesn't reset
                # row_factory, so mutating the pooled connection would poison the
                # next borrower with dict rows.
                row = conn.cursor(row_factory=dict_row).execute(
                    """
                    INSERT INTO users (household, username, role, password_hash, is_env_admin)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, household, username, role, password_hash
                    """,
                    (household, username, role, hash_password(password), is_env_admin),
                ).fetchone()
        except UniqueViolation as exc:
            raise DuplicateUser(username) from exc
        return self._row_to_user(row)

    def get_by_username(self, username: str) -> User | None:  # pragma: no cover
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            row = conn.cursor(row_factory=dict_row).execute(
                "SELECT id, household, username, role, password_hash FROM users"
                " WHERE lower(username) = lower(%s)",
                (username.strip(),),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_id(self, user_id: str) -> User | None:  # pragma: no cover
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            row = conn.cursor(row_factory=dict_row).execute(
                "SELECT id, household, username, role, password_hash FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_env_admin(self) -> User | None:  # pragma: no cover
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            row = conn.cursor(row_factory=dict_row).execute(
                "SELECT id, household, username, role, password_hash FROM users"
                " WHERE is_env_admin",
            ).fetchone()
        return self._row_to_user(row) if row else None

    def list_by_household(self, household: str) -> list[User]:  # pragma: no cover
        from psycopg.rows import dict_row

        with self._ensure_pool().connection() as conn:
            rows = conn.cursor(row_factory=dict_row).execute(
                "SELECT id, household, username, role, password_hash FROM users"
                " WHERE household = %s ORDER BY lower(username)",
                (household,),
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def delete(self, user_id: str) -> None:  # pragma: no cover
        with self._ensure_pool().connection() as conn:
            cur = conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
            if cur.rowcount == 0:
                raise KeyError(user_id)

    def authenticate(self, username: str, password: str) -> User | None:  # pragma: no cover
        user = self.get_by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            return None
        return user

    def update_credentials(  # pragma: no cover - requires a live database
        self,
        user_id: str,
        *,
        username: str | None = None,
        password: str | None = None,
        household: str | None = None,
    ) -> User:
        from psycopg.errors import UniqueViolation
        from psycopg.rows import dict_row

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
                    " RETURNING id, household, username, role, password_hash",
                    tuple(params),
                ).fetchone()
        except UniqueViolation as exc:
            raise DuplicateUser(username or "") from exc
        if row is None:
            raise KeyError(user_id)
        return self._row_to_user(row)
