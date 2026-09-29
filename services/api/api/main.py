"""FastAPI application.

Versioned under ``/api/v1``; the OpenAPI document is the contract the web client is
generated from (CLAUDE.md S10).

Two things every response gets, without a route having to remember them: an RFC 7807
problem document on any failure, and an audit entry for every access to vehicle,
location or driver data (PDF S11).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, Path, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.adapters.repository import FleetRepository
from api.deps import CORRELATION_HEADER, Authenticator, RateLimiter, RequestContext
from api.schemas import (
    FleetSummaryResponse,
    HealthResponse,
    IdleEvent,
    IdleEventPage,
    LeaderboardEntry,
    LeaderboardResponse,
    PageMeta,
    ReadyResponse,
    TrendPoint,
    TrendResponse,
    VehicleResponse,
)
from fleet_common.auth import TokenVerifier
from fleet_common.errors import (
    FleetError,
    PaginationCursorError,
    RateLimitedError,
    problem_from,
    unexpected_problem,
)
from fleet_common.pagination import (
    Cursor,
    InvalidCursorError,
    clamp_page_size,
)
from fleet_common.settings import (
    api_settings,
    app_settings,
    auth_settings,
    postgres_settings,
    redis_settings,
)

logger = logging.getLogger("api")

API_VERSION: Final[str] = "1.0.0"
PROBLEM_CONTENT_TYPE: Final[str] = "application/problem+json"

#: Default reporting window. 30 days matches the utilisation window and a monthly
#: finance cycle, which is the period a fleet finance manager actually reports on.
DEFAULT_WINDOW_DAYS: Final[int] = 30


def get_repository(request: Request) -> FleetRepository:
    """Resolve the repository from application state.

    A dependency rather than a global, so a test can substitute a fake with
    ``app.dependency_overrides`` and never touch a database.
    """
    repository: FleetRepository = request.app.state.repository
    return repository


def get_context(request: Request) -> RequestContext:
    """Authenticate, rate limit and build the request context.

    A route obtains a ``RequestContext`` or it does not run: authentication is not
    something an endpoint can forget to call.
    """
    authenticator: Authenticator = request.app.state.authenticator
    return authenticator(
        request,
        authorization=request.headers.get("authorization"),
        correlation=getattr(request.state, "correlation_id", ""),
    )


Ctx = Annotated[RequestContext, Depends(get_context)]
Repo = Annotated[FleetRepository, Depends(get_repository)]


def _problem_response(problem: Any, *, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.to_dict(),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers or {},
    )


def create_app() -> FastAPI:
    settings = api_settings()
    app_config = app_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Open pools on startup, close them on shutdown.

        Built here rather than at import time so the module can be imported for tests
        and OpenAPI generation without needing a live database.
        """
        repository = FleetRepository(postgres_settings())
        redis_client: Any = None
        try:
            from redis import Redis

            redis_client = Redis.from_url(
                redis_settings().redis_url, socket_timeout=2.0, socket_connect_timeout=2.0
            )
        except Exception as exc:
            # Redis backs the rate limiter and the response cache, neither of which the
            # API needs to answer correctly. Starting without it is better than not
            # starting at all (CLAUDE.md S10).
            logger.warning("Redis unavailable at startup; rate limiting disabled: %s", exc)

        verifier = TokenVerifier(auth_settings())
        application.state.repository = repository
        application.state.redis = redis_client
        application.state.authenticator = Authenticator(
            verifier,
            RateLimiter(redis_client, limit_per_minute=settings.api_rate_limit_per_minute),
            settings,
        )
        try:
            yield
        finally:
            repository.close()
            if redis_client is not None:
                redis_client.close()

    app = FastAPI(
        title="Fleet Fuel, Idling & Utilisation Cost Intelligence API",
        description=(
            "Turns connected-vehicle telemetry into the idle cost and utilisation "
            "figures a fleet finance team can act on.\n\n"
            "**Every monetary figure is an estimate** and is returned with the "
            "assumptions that produced it - formula version, burn rate, fuel price and "
            "idle threshold - so it can be reproduced by hand.\n\n"
            "Amounts are serialised as strings, not JSON numbers, because JSON numbers "
            "are IEEE 754 doubles in every JavaScript client and would accumulate error."
        ),
        version=API_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Authorization", "Content-Type", CORRELATION_HEADER],
        expose_headers=[CORRELATION_HEADER],
    )

    # ------------------------------------------------------------ middleware

    @app.middleware("http")
    async def correlate_and_time(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Attach a correlation id and a server-timing header to every response."""
        started = time.perf_counter()
        incoming = request.headers.get(CORRELATION_HEADER)
        correlation = incoming if incoming and incoming.isprintable() else None
        if correlation is None:
            import uuid

            correlation = str(uuid.uuid4())
        request.state.correlation_id = correlation

        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[CORRELATION_HEADER] = correlation
        response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
        return response

    # -------------------------------------------------------- error handlers

    @app.exception_handler(FleetError)
    async def handle_fleet_error(request: Request, exc: FleetError) -> JSONResponse:
        correlation = getattr(request.state, "correlation_id", "unknown")
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimitedError):
            headers["Retry-After"] = str(exc.retry_after_seconds)
        # Logged at the right level: a 4xx is the caller's problem and should not page
        # anyone, while a 5xx is ours.
        log = logger.warning if exc.status < 500 else logger.error
        log(
            "%s %s -> %d %s [%s]",
            request.method,
            request.url.path,
            int(exc.status),
            exc.problem_type,
            correlation,
        )
        return _problem_response(
            problem_from(exc, correlation_id=correlation, instance=request.url.path),
            headers=headers,
        )

    @app.exception_handler(InvalidCursorError)
    async def handle_bad_cursor(request: Request, exc: InvalidCursorError) -> JSONResponse:
        """Translate the domain's cursor error into a 400.

        The pagination module knows nothing about HTTP, so the translation happens here
        rather than letting a malformed cursor become a 500.
        """
        correlation = getattr(request.state, "correlation_id", "unknown")
        return _problem_response(
            problem_from(
                PaginationCursorError(str(exc)),
                correlation_id=correlation,
                instance=request.url.path,
            )
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Last resort. The response says nothing about the cause.

        The real exception goes to the log against the correlation id; putting its
        message in the body is how table names and query fragments leak (CLAUDE.md S10).
        """
        correlation = getattr(request.state, "correlation_id", "unknown")
        logger.exception("unhandled error on %s [%s]", request.url.path, correlation)
        return _problem_response(unexpected_problem(correlation, instance=request.url.path))

    # ------------------------------------------------------------- resources

    # ----------------------------------------------------------------- health

    @app.get("/healthz", response_model=HealthResponse, tags=["health"])
    def healthz() -> HealthResponse:
        """Liveness. Deliberately checks nothing external.

        A liveness probe that depends on the database would restart a perfectly healthy
        process during a database blip, turning one outage into two.
        """
        return HealthResponse(status="ok", service="api", version=API_VERSION)

    @app.get("/readyz", response_model=ReadyResponse, tags=["health"])
    def readyz(request: Request, response: Response) -> ReadyResponse:
        """Readiness. Checks the dependencies needed to serve real traffic."""
        repo: FleetRepository = request.app.state.repository
        postgres_ok = repo.check_ready()

        redis_ok = False
        redis_client = request.app.state.redis
        if redis_client is not None:
            try:
                redis_ok = bool(redis_client.ping())
            except Exception:
                redis_ok = False

        # Postgres is required; Redis is not. Without Redis the API still answers
        # correctly, only without rate limiting, so it reports degraded rather than
        # unready and stays in the load balancer.
        if not postgres_ok:
            response.status_code = 503
            status = "unready"
        elif not redis_ok:
            status = "degraded"
        else:
            status = "ready"

        return ReadyResponse(
            status=status,
            postgres=postgres_ok,
            redis=redis_ok,
            degraded=postgres_ok and not redis_ok,
        )

    # ------------------------------------------------------------- api routes

    @app.get(
        "/api/v1/fleet/summary",
        response_model=FleetSummaryResponse,
        tags=["fleet"],
        summary="Fleet-level idle cost and utilisation KPIs",
    )
    def fleet_summary(
        ctx: Ctx,
        repo: Repo,
        days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_WINDOW_DAYS,
    ) -> FleetSummaryResponse:
        until = datetime.now(UTC)
        since = until - timedelta(days=days)
        summary = repo.fleet_summary(ctx.principal, since=since, until=until)
        repo.record_access(
            ctx.principal,
            action="read_fleet_summary",
            resource_type="fleet",
            resource_id=None,
            correlation_id=ctx.correlation_id,
            client_ip_masked=ctx.client_ip_masked,
        )
        return FleetSummaryResponse.from_summary(summary)

    @app.get(
        "/api/v1/idle-events",
        response_model=IdleEventPage,
        tags=["idle"],
        summary="Idle events, newest first, keyset-paginated",
    )
    def list_idle_events(
        ctx: Ctx,
        repo: Repo,
        cursor: Annotated[
            str | None, Query(description="Opaque cursor from a previous page")
        ] = None,
        page_size: Annotated[int | None, Query(ge=1, le=200)] = None,
        vin: Annotated[str | None, Query(min_length=17, max_length=17)] = None,
        excessive_only: bool = False,
        days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_WINDOW_DAYS,
    ) -> IdleEventPage:
        size = clamp_page_size(page_size, default=api_settings().api_page_size_default)
        decoded = Cursor.decode(cursor) if cursor else None
        page = repo.list_idle_events(
            ctx.principal,
            page_size=size,
            cursor=decoded,
            vin=vin,
            excessive_only=excessive_only,
            since=datetime.now(UTC) - timedelta(days=days),
        )
        repo.record_access(
            ctx.principal,
            action="read_idle_events",
            resource_type="idle_event",
            resource_id=vin,
            correlation_id=ctx.correlation_id,
            client_ip_masked=ctx.client_ip_masked,
        )
        coarsened = not ctx.principal.may_see_precise_location
        return IdleEventPage(
            items=[IdleEvent.from_row(row, location_coarsened=coarsened) for row in page.items],
            page=PageMeta(
                next_cursor=page.next_cursor,
                page_size=size,
                returned=page.size,
                has_more=page.has_more,
            ),
        )

    @app.get(
        "/api/v1/leaderboard/idle-cost",
        response_model=LeaderboardResponse,
        tags=["idle"],
        summary="Top vehicles by estimated idle cost",
    )
    def idle_cost_leaderboard(
        ctx: Ctx,
        repo: Repo,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_WINDOW_DAYS,
    ) -> LeaderboardResponse:
        since = datetime.now(UTC) - timedelta(days=days)
        rows = repo.idle_cost_leaderboard(ctx.principal, since=since, limit=limit)
        repo.record_access(
            ctx.principal,
            action="read_idle_leaderboard",
            resource_type="fleet",
            resource_id=None,
            correlation_id=ctx.correlation_id,
            client_ip_masked=ctx.client_ip_masked,
        )
        return LeaderboardResponse(
            items=[LeaderboardEntry.from_row(row, rank) for rank, row in enumerate(rows, 1)],
            since=since,
        )

    @app.get(
        "/api/v1/vehicles/{vin}",
        response_model=VehicleResponse,
        tags=["vehicles"],
        summary="Vehicle detail with idle totals",
    )
    def vehicle_detail(
        ctx: Ctx,
        repo: Repo,
        vin: Annotated[str, Path(min_length=17, max_length=17)],
    ) -> VehicleResponse:
        detail = repo.vehicle_detail(ctx.principal, vin)
        repo.record_access(
            ctx.principal,
            action="read_vehicle",
            resource_type="vehicle",
            resource_id=vin,
            correlation_id=ctx.correlation_id,
            client_ip_masked=ctx.client_ip_masked,
        )
        return VehicleResponse.from_detail(detail)

    @app.get(
        "/api/v1/fleet/trend",
        response_model=TrendResponse,
        tags=["fleet"],
        summary="Daily idle hours and cost",
    )
    def fleet_trend(
        ctx: Ctx,
        repo: Repo,
        days: Annotated[int, Query(ge=2, le=365)] = DEFAULT_WINDOW_DAYS,
    ) -> TrendResponse:
        until = datetime.now(UTC).date() + timedelta(days=1)
        since = until - timedelta(days=days)
        rows = repo.daily_idle_trend(ctx.principal, since=since, until=until)
        repo.record_access(
            ctx.principal,
            action="read_fleet_trend",
            resource_type="fleet",
            resource_id=None,
            correlation_id=ctx.correlation_id,
            client_ip_masked=ctx.client_ip_masked,
        )
        from fleet_common.settings import cost_settings

        return TrendResponse(
            items=[
                TrendPoint(day=day, idle_hours=round(hours, 2), cost=cost)
                for day, hours, cost in rows
            ],
            currency=cost_settings().default_currency,
        )

    logger.info("API created: env=%s version=%s", app_config.app_env, API_VERSION)
    return app


app = create_app()
