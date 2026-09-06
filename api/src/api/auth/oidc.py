"""Authentik OIDC access-token validation — the API as an OAuth2 resource server.

Second auth backend (XERK-649, T3), alongside — never replacing — the built-in
HMAC bearer tokens (``tokens.py``). Off unless ``API_OIDC_ENABLED`` is set; the
shared design is ``docs/auth-oidc.md``.

The token the API validates is an Authentik **access token**: a JWT signed
**RS256**, verified **locally** against the provider's JWKS (no userinfo /
introspection round-trip on the hot path). We pin the algorithm to RS256 so
``alg: none`` and symmetric-key confusion with the HMAC backend are impossible,
check ``iss``/``aud``/``exp``/``nbf``, and extract the claims T4's account
linking needs (``sub``, ``email``, ``email_verified``, ``groups`` → role) onto
the same :class:`Principal` seam the built-in path produces.

JWKS keys are cached in-process and re-fetched on an unknown ``kid`` so Authentik
key rotation is picked up without a restart (a new signing key just misses the
cache once and triggers a refetch). The HTTP fetch is injectable so the verifier
unit-tests run against a locally-generated RSA keypair with no network.
"""

from __future__ import annotations

from typing import Callable

import httpx
import jwt
from jwt import PyJWK

from api.auth.tokens import AuthError, Principal
from api.config import Settings

# The JWKS document is small and rarely changes; a short HTTP timeout keeps a slow
# or hung IdP from stalling an authenticated request. A miss still refetches.
_JWKS_FETCH_TIMEOUT_SECONDS = 5.0

# What a JWKS fetch returns: the parsed JSON document, i.e. ``{"keys": [ ... ]}``.
JwksFetcher = Callable[[], dict]


class OidcVerifier:
    """Validates Authentik OIDC access tokens and maps them to a :class:`Principal`.

    Holds the in-process JWKS cache (keyed by ``kid``), so one verifier instance is
    kept for the process lifetime (see ``deps.get_oidc_verifier``) and survives key
    rotation without a restart.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        algorithms: list[str],
        groups_claim: str,
        admin_group: str,
        email_claim: str,
        email_verified_claim: str,
        household: str,
        leeway_seconds: int = 60,
        fetch_jwks: JwksFetcher | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._algorithms = list(algorithms)
        self._groups_claim = groups_claim
        self._admin_group = admin_group
        self._email_claim = email_claim
        self._email_verified_claim = email_verified_claim
        self._household = household
        self._leeway = leeway_seconds
        self._fetch = fetch_jwks or self._http_fetch
        self._keys: dict[str, PyJWK] = {}

    @classmethod
    def from_settings(
        cls, settings: Settings, *, fetch_jwks: JwksFetcher | None = None
    ) -> "OidcVerifier":
        return cls(
            issuer=settings.oidc_issuer.strip(),
            audience=settings.oidc_audience.strip(),
            jwks_url=settings.oidc_jwks_url_resolved,
            algorithms=settings.oidc_algorithm_list,
            groups_claim=settings.oidc_groups_claim,
            admin_group=settings.oidc_admin_group,
            email_claim=settings.oidc_email_claim,
            email_verified_claim=settings.oidc_email_verified_claim,
            household=settings.auth_admin_household,
            leeway_seconds=settings.oidc_leeway_seconds,
            fetch_jwks=fetch_jwks,
        )

    # --- JWKS cache ----------------------------------------------------------

    def _http_fetch(self) -> dict:
        resp = httpx.get(self._jwks_url, timeout=_JWKS_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json()

    def _refresh_keys(self) -> None:
        """Re-fetch the JWKS and rebuild the kid→key cache.

        Any transport/parse failure surfaces as an ``AuthError`` (⇒ 401): an
        unverifiable token is never accepted just because the key server blipped.
        """
        try:
            document = self._fetch()
            keys = {
                key["kid"]: PyJWK.from_dict(key)
                for key in document.get("keys", [])
                if key.get("kid")
            }
        except Exception as exc:
            # Fail closed: any transport/parse failure loading the keys means the
            # token cannot be verified, so it is rejected (⇒ 401) — never accepted,
            # and never a 500. A down JWKS server must not crash an auth check.
            raise AuthError(f"could not load OIDC signing keys: {exc}") from exc
        self._keys = keys

    def _key_for_kid(self, kid: str) -> PyJWK:
        """The signing key for ``kid``, refetching once on a cache miss.

        A rotated-in Authentik key is unknown to the cache exactly once; the miss
        triggers a refresh and the next lookup finds it — rotation with no restart.
        """
        if kid not in self._keys:
            self._refresh_keys()
        key = self._keys.get(kid)
        if key is None:
            raise AuthError("unknown OIDC signing key (kid)")
        return key

    # --- verification --------------------------------------------------------

    def verify(self, token: str) -> Principal:
        """Verify an Authentik access token, or raise :class:`AuthError` (⇒ 401)."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("malformed OIDC token header") from exc
        alg = header.get("alg")
        if alg not in self._algorithms:
            # Pinned RS256: rejects alg:none and any symmetric alg outright, so the
            # OIDC verifier can never be tricked into running the HMAC backend's alg.
            raise AuthError(f"unexpected OIDC token alg: {alg!r}")
        kid = header.get("kid")
        if not kid:
            raise AuthError("OIDC token missing kid")
        key = self._key_for_kid(kid)
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid OIDC token: {exc}") from exc
        return self._principal_of(claims)

    def _principal_of(self, claims: dict) -> Principal:
        sub = str(claims.get("sub", ""))
        if not sub:
            raise AuthError("OIDC token missing sub")
        raw_groups = claims.get(self._groups_claim)
        groups = tuple(str(g) for g in raw_groups) if isinstance(raw_groups, list) else ()
        role = "admin" if self._admin_group in groups else "member"
        email = claims.get(self._email_claim)
        # Only a literal boolean ``true`` counts as verified — a truthy string or a
        # missing claim must NOT open T4's verified-email link (docs/auth-oidc.md §5).
        email_verified = claims.get(self._email_verified_claim) is True
        username = str(claims.get("preferred_username") or claims.get("name") or "")
        return Principal(
            # user_id is the subject in T3; T4 resolves it to the local users.id
            # before ownership/liveness checks key on it.
            user_id=sub,
            household=self._household,
            role=role,
            username=username,
            sub=sub,
            email=str(email) if email is not None else None,
            email_verified=email_verified,
            groups=groups,
        )
