"""OIDC/JWT validation and RBAC (D4).

No vendor appears anywhere in this module, deliberately. D4 is still open, so the code
validates a standard OIDC token against a JWKS endpoint and the provider is chosen
entirely by environment configuration. Swapping Cognito for Keycloak for the local stub
is a change to three environment variables, not to any code.

**The algorithm allow-list is the load-bearing control here.** Accepting ``none``
disables verification entirely; accepting an HMAC algorithm against a JWKS issuer
enables algorithm confusion, where an attacker signs a token using the issuer's
*public* key as the shared secret and it verifies. Both are complete authentication
bypasses, so the allow-list is enforced twice: once when settings load, and again at
verification time in case a key set advertises something unexpected.

Every claim the application trusts comes from the verified token. Nothing is read from
a request body, query string or header (CLAUDE.md S3).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import httpx
import jwt
from jwt import PyJWKClient

from fleet_common.errors import ForbiddenError, UnauthenticatedError
from fleet_common.settings import AuthSettings

#: Algorithms that must never be accepted, whatever configuration says.
FORBIDDEN_ALGORITHMS: Final[frozenset[str]] = frozenset({"none", "NONE", "HS256", "HS384", "HS512"})

#: Small leeway for clock skew between the issuer and this service. Without it, a
#: correctly issued token can be rejected for a second or two after issuance.
CLOCK_SKEW_LEEWAY_SECONDS: Final[int] = 30


class Role(StrEnum):
    """Application roles (PDF S11: RBAC).

    Ordered by breadth of access. ``FINANCE_MANAGER`` and ``OPS_MANAGER`` are the two
    primary personas; the others are secondary and marked unvalidated in the solution
    document rather than presented as researched.
    """

    FINANCE_MANAGER = "finance_manager"
    OPS_MANAGER = "ops_manager"
    SUPERVISOR = "supervisor"
    ANALYST = "analyst"
    ADMIN = "admin"

    @property
    def may_see_precise_location(self) -> bool:
        """Whether this role receives full-precision coordinates.

        PDF S11 and CLAUDE.md S11 require location masking for roles that do not need
        precision. Finance and analyst roles work with cost and utilisation, not with
        where a driver physically is, so they get coarsened coordinates - which is
        data minimisation applied at the point of access rather than promised in a
        document.
        """
        return self in (Role.OPS_MANAGER, Role.SUPERVISOR, Role.ADMIN)

    @property
    def may_erase_data(self) -> bool:
        """Right-to-erasure is destructive and audited, so it is admin-only."""
        return self is Role.ADMIN


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    ``tenant_id`` comes from the verified token and nowhere else. This is the single
    value that keeps tenants apart, and accepting it from a request parameter would
    make every RLS policy in the database decorative.
    """

    subject: str
    tenant_id: str
    role: Role
    issued_at: int
    expires_at: int

    @property
    def may_see_precise_location(self) -> bool:
        return self.role.may_see_precise_location

    def require_role(self, *allowed: Role) -> None:
        """Raise unless the principal holds one of ``allowed``."""
        if self.role not in allowed and self.role is not Role.ADMIN:
            raise ForbiddenError(f"role {self.role.value!r} may not perform this action")


class JwksCache:
    """Caches the issuer's signing keys.

    Fetching JWKS per request would add a network round trip to every call and make the
    API's availability depend on the identity provider's. Cached with a TTL, so key
    rotation is picked up without a restart.
    """

    def __init__(self, settings: AuthSettings) -> None:
        self._settings = settings
        self._client: PyJWKClient | None = None
        self._fetched_at = 0.0

    def _expired(self) -> bool:
        return time.monotonic() - self._fetched_at > self._settings.oidc_jwks_cache_seconds

    def client(self) -> PyJWKClient:
        if self._client is None or self._expired():
            self._client = PyJWKClient(
                self._settings.oidc_jwks_url,
                cache_keys=True,
                # Bounded: an unreachable provider must fail the request quickly
                # rather than hold a worker until the client times out (CLAUDE.md S7).
                timeout=5,
            )
            self._fetched_at = time.monotonic()
        return self._client

    def invalidate(self) -> None:
        """Force a refetch, e.g. after a signature failure that a rotation explains."""
        self._client = None


class TokenVerifier:
    """Verifies bearer tokens and maps them to a ``Principal``."""

    def __init__(self, settings: AuthSettings, *, jwks: JwksCache | None = None) -> None:
        self._settings = settings
        self._jwks = jwks or JwksCache(settings)
        self._algorithms = self._safe_algorithms(settings.allowed_algorithms)

    @staticmethod
    def _safe_algorithms(configured: list[str]) -> list[str]:
        """Filter the configured list again at construction.

        Settings validation already refuses unsafe algorithms. This is the second
        check: defence in depth on a control whose failure is a total bypass, and it
        costs nothing.
        """
        safe = [a for a in configured if a not in FORBIDDEN_ALGORITHMS]
        if not safe:
            raise ValueError("no safe JWT algorithms configured; refusing to verify tokens")
        return safe

    def verify(self, token: str) -> Principal:
        """Verify a bearer token and extract the principal.

        Raises:
            UnauthenticatedError: for any verification failure. The reason is
                deliberately not distinguished to the caller - telling an attacker
                whether the signature or the audience failed tells them which half of
                a forgery attempt worked.
        """
        if not token:
            raise UnauthenticatedError("no bearer token was supplied")

        try:
            signing_key = self._jwks.client().get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.PyJWKClientError as exc:
            # Often a rotated key: drop the cache so the next attempt refetches.
            self._jwks.invalidate()
            raise UnauthenticatedError("token signing key could not be resolved") from exc
        except jwt.InvalidTokenError as exc:
            raise UnauthenticatedError("token is not valid") from exc
        except httpx.HTTPError as exc:
            raise UnauthenticatedError("identity provider is unreachable") from exc

        return self._to_principal(claims)

    @staticmethod
    def _to_principal(claims: dict[str, Any]) -> Principal:
        """Map verified claims to a principal, refusing anything incomplete."""
        tenant_id = claims.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            # Without a tenant the request has no scope. Refusing beats defaulting:
            # a default tenant would silently expose one tenant's data to everyone.
            raise UnauthenticatedError("token has no tenant_id claim")

        raw_role = claims.get("role")
        if not isinstance(raw_role, str):
            raise ForbiddenError("token has no role claim")
        try:
            role = Role(raw_role)
        except ValueError as exc:
            raise ForbiddenError(f"unknown role {raw_role!r}") from exc

        return Principal(
            subject=str(claims["sub"]),
            tenant_id=tenant_id,
            role=role,
            issued_at=int(claims["iat"]),
            expires_at=int(claims["exp"]),
        )


def extract_bearer(authorization_header: str | None) -> str:
    """Pull the token out of an ``Authorization`` header.

    Raises:
        UnauthenticatedError: if the header is absent or not a bearer scheme.
    """
    if not authorization_header:
        raise UnauthenticatedError("Authorization header is missing")
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise UnauthenticatedError("Authorization header must use the Bearer scheme")
    token = parts[1].strip()
    if not token:
        raise UnauthenticatedError("bearer token is empty")
    return token


def coarsen_location(
    lat: float | None, lon: float | None, *, precise: bool
) -> tuple[float | None, float | None]:
    """Reduce coordinate precision for roles that do not need it.

    Two decimal places is roughly a 1 km cell: enough to say which depot or city a
    vehicle was in, not enough to identify a street address or follow an individual.
    Applied at the point of access, so a role change takes effect immediately without
    rewriting stored data (CLAUDE.md S11).
    """
    if precise or lat is None or lon is None:
        return lat, lon
    return round(lat, 2), round(lon, 2)
