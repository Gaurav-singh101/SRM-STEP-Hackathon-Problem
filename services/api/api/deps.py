"""Request-scoped dependencies: authentication, rate limiting, correlation ids.

Everything here runs before a route body. Authentication and tenant scope are not
optional extras a route can forget: a route obtains a ``Principal`` or it does not run.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Depends, Header, Request

from fleet_common.auth import Principal, Role, TokenVerifier, extract_bearer
from fleet_common.errors import RateLimitedError
from fleet_common.settings import ApiSettings

logger = logging.getLogger(__name__)

#: Header a caller may supply to trace a request across services. Generated when absent
#: so every request has one - a log line with no correlation id is unactionable.
CORRELATION_HEADER: Final[str] = "X-Correlation-Id"


def correlation_id(
    incoming: Annotated[str | None, Header(alias=CORRELATION_HEADER)] = None,
) -> str:
    """Use the caller's correlation id, or mint one.

    A caller-supplied value is echoed but never trusted for anything but logging: it is
    unauthenticated input, so it must not reach a query or a decision.
    """
    if incoming and 8 <= len(incoming) <= 128 and incoming.isprintable():
        return incoming
    return str(uuid.uuid4())


def mask_client_ip(request: Request) -> str | None:
    """Truncate the client address before it is stored.

    A /24 for IPv4 and a /48 for IPv6: enough to investigate abuse from a network, not
    enough to identify an individual. PDF S11 requires data minimisation, and an audit
    log holding full addresses for every read would itself be the privacy problem.
    """
    client = request.client
    if client is None:
        return None
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        return str(ipaddress.ip_network(f"{address}/24", strict=False))
    return str(ipaddress.ip_network(f"{address}/48", strict=False))


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Everything a route needs about who is calling and why."""

    principal: Principal
    correlation_id: str
    client_ip_masked: str | None
    path: str

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    def require_role(self, *allowed: Role) -> None:
        self.principal.require_role(*allowed)


class RateLimiter:
    """Redis-backed fixed-window rate limiter, per tenant and subject.

    Keyed on the tenant *and* the subject so one noisy user cannot exhaust their whole
    organisation's budget, and one tenant cannot exhaust another's.

    Fails **open** when Redis is unavailable. That is a deliberate trade-off and worth
    stating: an outage in a protective component should not take the API down with it.
    The cost is that a Redis outage temporarily removes rate limiting, which is
    acceptable because the limiter protects against load, not against attackers who
    would already be authenticated here.
    """

    def __init__(self, redis_client: object | None, *, limit_per_minute: int) -> None:
        self._redis = redis_client
        self._limit = limit_per_minute

    def check(self, principal: Principal) -> None:
        """Raise ``RateLimitedError`` when the caller is over budget."""
        if self._redis is None or self._limit <= 0:
            return

        import time

        window = int(time.time() // 60)
        key = f"rl:{principal.tenant_id}:{principal.subject}:{window}"

        try:
            pipe = self._redis.pipeline(transaction=False)  # type: ignore[attr-defined]
            pipe.incr(key)
            # Expire slightly beyond the window so a key cannot outlive its usefulness
            # and slowly fill Redis with dead counters.
            pipe.expire(key, 120)
            count = int(pipe.execute()[0])
        except Exception as exc:
            logger.warning("rate limiter unavailable, allowing request: %s", exc)
            return

        if count > self._limit:
            # Retry-After so a client waits rather than retrying immediately and making
            # the overload worse.
            raise RateLimitedError(
                retry_after_seconds=60 - int(time.time() % 60),
                detail=f"limit of {self._limit} requests per minute exceeded",
            )


class Authenticator:
    """Turns an Authorization header into a ``RequestContext``."""

    def __init__(
        self, verifier: TokenVerifier, rate_limiter: RateLimiter, settings: ApiSettings
    ) -> None:
        self._verifier = verifier
        self._rate_limiter = rate_limiter
        self._settings = settings

    def __call__(
        self,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        correlation: Annotated[str, Depends(correlation_id)] = "",
    ) -> RequestContext:
        # Verify first, then rate limit: an unauthenticated request must not be able to
        # consume another principal's budget.
        principal = self._verifier.verify(extract_bearer(authorization))
        self._rate_limiter.check(principal)

        return RequestContext(
            principal=principal,
            correlation_id=correlation,
            client_ip_masked=mask_client_ip(request),
            path=request.url.path,
        )
