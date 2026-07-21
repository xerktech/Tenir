"""Auth & household tenancy seam (master plan §7, decision #6, Phase 6).

Built-in multi-user auth: login issues a signed bearer token, and a single
``current_principal`` dependency scopes every REST request (and the WS session) to
the caller's household. Auth is always required — there is no no-login mode.
Everything is stdlib-only so it runs in CI with no extra deps.
"""

from __future__ import annotations

from api.auth.deps import (
    assert_secure_auth_config,
    current_principal,
    principal_from_token,
    require_admin,
)
from api.auth.tokens import (
    AuthError,
    Principal,
    decode_token,
    hash_password,
    issue_token,
    sign_message,
    verify_password,
)
from api.auth.users import User, get_user_store, reset_user_store

__all__ = [
    "AuthError",
    "Principal",
    "User",
    "assert_secure_auth_config",
    "current_principal",
    "decode_token",
    "get_user_store",
    "hash_password",
    "issue_token",
    "principal_from_token",
    "require_admin",
    "reset_user_store",
    "sign_message",
    "verify_password",
]
