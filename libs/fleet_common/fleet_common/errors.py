"""Typed errors and RFC 7807 problem responses.

CLAUDE.md S10 requires consistent ``application/problem+json`` errors that never leak
stack traces or internal detail. Both halves matter: a machine-readable shape so a
client can branch on ``type`` rather than string-matching prose, and a hard boundary so
an exception message never reaches a caller verbatim.

Every error carries a ``correlation_id``. Without one, "the API returned 500" is
unactionable; with one, the exact request is findable in the logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Final

#: Base for problem type URIs. Stable identifiers, so a client can match on them.
PROBLEM_BASE: Final[str] = "https://fleet-intel.local/problems"


class FleetError(Exception):
    """Base for every error this application raises deliberately.

    Carries the HTTP status and a stable problem type, so the transport layer maps it
    without a chain of isinstance checks, and an unmapped exception is visibly a bug
    rather than quietly becoming a 500 with a leaked message.
    """

    status: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR
    problem_type: str = "internal-error"
    #: Shown to the caller. Must never contain a value, a query, or a stack frame.
    title: str = "An unexpected error occurred"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra


class NotFoundError(FleetError):
    status = HTTPStatus.NOT_FOUND
    problem_type = "not-found"
    title = "Resource not found"


class ValidationFailedError(FleetError):
    status = HTTPStatus.BAD_REQUEST
    problem_type = "validation-failed"
    title = "Request validation failed"


class PaginationCursorError(FleetError):
    """HTTP-facing counterpart of ``pagination.InvalidCursorError``.

    Two types on purpose: the domain raises its own ``ValueError`` subclass with no
    knowledge of HTTP, and the transport layer translates it here. That keeps the
    pagination module free of status codes and problem URIs, and it means a bad cursor
    answers 400 rather than becoming a 500.
    """

    status = HTTPStatus.BAD_REQUEST
    problem_type = "invalid-cursor"
    title = "Pagination cursor is invalid"


class UnauthenticatedError(FleetError):
    status = HTTPStatus.UNAUTHORIZED
    problem_type = "unauthenticated"
    #: Deliberately vague. Distinguishing "no token" from "expired" from "bad
    #: signature" tells an attacker which part of a forgery attempt worked.
    title = "Authentication is required"


class ForbiddenError(FleetError):
    status = HTTPStatus.FORBIDDEN
    problem_type = "forbidden"
    title = "Insufficient permissions for this resource"


class RateLimitedError(FleetError):
    status = HTTPStatus.TOO_MANY_REQUESTS
    problem_type = "rate-limited"
    title = "Rate limit exceeded"

    def __init__(self, retry_after_seconds: int, detail: str | None = None) -> None:
        super().__init__(detail)
        #: Surfaced as the Retry-After header. Without it a client can only guess,
        #: and guessing usually means retrying immediately and making it worse.
        self.retry_after_seconds = retry_after_seconds


class DependencyUnavailableError(FleetError):
    status = HTTPStatus.SERVICE_UNAVAILABLE
    problem_type = "dependency-unavailable"
    title = "A downstream dependency is unavailable"


@dataclass(frozen=True, slots=True)
class Problem:
    """An RFC 7807 problem document."""

    type: str
    title: str
    status: int
    detail: str
    correlation_id: str
    instance: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": f"{PROBLEM_BASE}/{self.type}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "correlation_id": self.correlation_id,
        }
        if self.instance:
            body["instance"] = self.instance
        body.update(self.extra)
        return body


def problem_from(error: FleetError, *, correlation_id: str, instance: str | None = None) -> Problem:
    """Build a problem document from a typed error."""
    return Problem(
        type=error.problem_type,
        title=error.title,
        status=int(error.status),
        detail=error.detail,
        correlation_id=correlation_id,
        instance=instance,
        extra=dict(error.extra),
    )


def unexpected_problem(correlation_id: str, *, instance: str | None = None) -> Problem:
    """Problem document for an unhandled exception.

    Deliberately says nothing. The real cause is logged against the correlation id;
    putting an exception message here is how table names, file paths and query
    fragments end up in front of callers (CLAUDE.md S10).
    """
    return Problem(
        type="internal-error",
        title="An unexpected error occurred",
        status=int(HTTPStatus.INTERNAL_SERVER_ERROR),
        detail=(
            "The request could not be completed. Quote the correlation id when reporting this."
        ),
        correlation_id=correlation_id,
        instance=instance,
    )
