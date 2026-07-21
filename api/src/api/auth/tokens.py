"""Principal, password hashing, and bearer tokens (master plan §7, Phase 6).

Auth is deliberately dependency-free: the household instance is self-hosted and
small (decision #6), so rather than pull in a JWT/crypto stack we sign a compact
bearer token with stdlib HMAC-SHA256 and hash passwords with PBKDF2. The token is
a JWT-shaped ``<payload>.<signature>`` pair (base64url, no alg confusion possible
because only one alg is ever accepted), carrying the user id, household and role
plus an expiry. Everything here is pure-Python and runs in CI.

The same ``Principal`` is what the FastAPI dependency (``deps.py``) hands every
request, and what the WS endpoint scopes a session to — so household tenancy is
enforced in exactly one place.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Literal

Role = Literal["member", "admin"]

# OWASP guidance for PBKDF2-HMAC-SHA256 (600k iterations). The stored hash records
# its own iteration count, so existing hashes still verify if this is ever changed.
_PBKDF2_ITERATIONS = 600_000


class AuthError(Exception):
    """Raised when a token is missing, malformed, tampered with, or expired."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: who they are, their household, and their role."""

    user_id: str
    household: str
    role: Role = "member"
    username: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


# --- password hashing --------------------------------------------------------


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """Hash a password as ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` (hex)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a password against a ``hash_password`` string."""
    try:
        scheme, iter_s, salt_hex, hash_hex = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iter_s)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# --- bearer tokens -----------------------------------------------------------


def _sign(payload_b64: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return _b64url_encode(sig)


def sign_message(message: str, secret: str) -> str:
    """HMAC-SHA256 sign an arbitrary message, returned base64url.

    The same primitive that signs bearer tokens, exposed for the webhook dispatcher
    so an outgoing delivery carries a signature a receiver can verify with the shared
    subscription secret (there is exactly one signing algorithm, so no alg confusion).
    """
    return _sign(message, secret)


def issue_token(
    principal: Principal, *, secret: str, ttl_seconds: int, now: float | None = None
) -> str:
    """Sign a bearer token for ``principal`` that expires after ``ttl_seconds``."""
    issued = int(now if now is not None else time.time())
    claims = {
        "sub": principal.user_id,
        "un": principal.username,
        "hh": principal.household,
        "role": principal.role,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    payload_b64 = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def decode_token(token: str, *, secret: str, now: float | None = None) -> Principal:
    """Verify a bearer token's signature + expiry and return its ``Principal``."""
    try:
        payload_b64, sig = token.split(".")
    except ValueError as exc:
        raise AuthError("malformed token") from exc
    if not hmac.compare_digest(sig, _sign(payload_b64, secret)):
        raise AuthError("bad token signature")
    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("malformed token payload") from exc
    if claims.get("exp", 0) < (now if now is not None else time.time()):
        raise AuthError("token expired")
    role = claims.get("role")
    return Principal(
        user_id=str(claims.get("sub", "")),
        household=str(claims.get("hh", "")),
        role="admin" if role == "admin" else "member",
        username=str(claims.get("un", "")),
    )
