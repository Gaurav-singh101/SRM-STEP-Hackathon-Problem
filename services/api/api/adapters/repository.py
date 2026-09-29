"""PostgreSQL read adapter.

Tenant isolation is applied here, not in the routers. Every connection checked out of
the pool does ``SET ROLE fleet_app`` and sets ``app.tenant_id`` from the *verified*
token before any query runs, so the database refuses cross-tenant rows even if a query
in this file forgot its filter (CLAUDE.md S10, ADR-0006).

All list queries use keyset pagination on a total ordering. Sort and filter fields come
from allow-lists, never from caller input, so no request can shape SQL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

import psycopg
import psycopg.rows
from psycopg_pool import ConnectionPool

from fleet_common.auth import Principal, coarsen_location
from fleet_common.errors import DependencyUnavailableError, NotFoundError
from fleet_common.pagination import Cursor, Page, build_page
from fleet_common.settings import PostgresSettings

logger = logging.getLogger(__name__)

Row = tuple[Any, ...]

#: Allow-listed sort columns for the idle-events list. A caller supplies a key from
#: this map, never a column name, so a request cannot inject SQL or sort by a column
#: with no index and turn a cheap query into a table scan (CLAUDE.md S10).
IDLE_SORT_COLUMNS: Final[dict[str, str]] = {
    "started_at": "started_at",
    "duration": "duration_seconds",
    "cost": "cost_est",
}


@dataclass(frozen=True, slots=True)
class IdleEventRow:
    """One idle event, as the API returns it."""

    idle_event_id: int
    vin: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    sample_count: int
    max_gap_seconds: float
    excessive: bool
    pto_active_any: bool
    start_lat: float | None
    start_lon: float | None
    fuel_litres_est: float | None
    cost_est: Decimal | None
    currency: str | None
    # --- assumptions, so the caller can reproduce the figure (CLAUDE.md S14) ---
    formula_version: str
    burn_rate_lph: float | None
    price_id: str | None
    idle_threshold_seconds: int
    fuel_litres_measured: float | None
    #: True when this vehicle is a BEV and therefore has no fuel cost (D9).
    bev_excluded: bool


@dataclass(frozen=True, slots=True)
class FleetSummary:
    """Fleet-level KPIs for the overview screen."""

    tenant_id: str
    window_from: datetime
    window_to: datetime
    vehicle_count: int
    vehicles_with_idle: int
    idle_event_count: int
    excessive_count: int
    total_idle_hours: float
    total_fuel_litres: float
    total_cost: Decimal
    currency: str
    #: Vehicles excluded from the cost total because they are BEVs (D9). The UI states
    #: this next to the figure: a total that silently omits vehicles is a wrong total.
    excluded_bev_count: int
    formula_version: str
    idle_threshold_seconds: int
    open_alert_count: int


@dataclass(frozen=True, slots=True)
class LeaderboardRow:
    """One vehicle on the idle-cost leaderboard."""

    vin: str
    registration: str
    vehicle_class: str
    powertrain: str
    fleet_name: str
    idle_event_count: int
    excessive_count: int
    total_idle_hours: float
    total_fuel_litres: float
    total_cost: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class VehicleDetail:
    """Vehicle master data plus its idle totals."""

    vin: str
    registration: str
    tenant_id: str
    fleet_name: str
    vehicle_class: str
    powertrain: str
    fuel_type: str | None
    model_year: int
    is_active: bool
    burns_fuel: bool
    idle_event_count: int
    total_idle_hours: float
    total_cost: Decimal | None
    currency: str | None


class FleetRepository:
    """Read-side data access for the API."""

    def __init__(self, settings: PostgresSettings) -> None:
        self._settings = settings
        self._pool = ConnectionPool(
            conninfo=settings.dsn,
            min_size=settings.postgres_pool_min,
            max_size=settings.postgres_pool_max,
            kwargs={"row_factory": psycopg.rows.tuple_row},
            # Bounded wait: a request must fail fast rather than hold a worker behind
            # an exhausted pool until the client gives up (CLAUDE.md S7).
            timeout=5.0,
            open=True,
        )

    def close(self) -> None:
        self._pool.close()

    def check_ready(self) -> bool:
        """Readiness probe: can we actually reach the database?"""
        try:
            with self._pool.connection(timeout=2.0) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() is not None
        except (psycopg.Error, TimeoutError):
            return False

    @contextmanager
    def _scoped(self, principal: Principal) -> Iterator[psycopg.Cursor[Row]]:
        """A cursor bound to the caller's tenant and least-privilege role.

        This is the single place tenant scope is applied. Doing it on checkout rather
        than per query means a query written without a tenant filter returns nothing
        instead of another tenant's rows.
        """
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SET ROLE fleet_app")
                # set_config, not SET: SET accepts no bind parameters, so applying a
                # tenant with it would mean interpolating a claim into SQL.
                cur.execute("SELECT set_config('app.tenant_id', %s, false)", (principal.tenant_id,))
                cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(self._settings.postgres_statement_timeout_ms),),
                )
                yield cur
        except psycopg.OperationalError as exc:
            logger.error("database unavailable: %s", exc)
            raise DependencyUnavailableError("the database is unavailable") from exc

    # ---------------------------------------------------------------- audit

    def record_access(
        self,
        principal: Principal,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None,
        correlation_id: str,
        client_ip_masked: str | None,
    ) -> None:
        """Append an audit entry.

        PDF S11 requires one for every access to vehicle, location or driver data. The
        application role has INSERT but not UPDATE or DELETE on this table, so the
        trail cannot be rewritten by the code that writes it (ADR-0006).
        """
        with self._scoped(principal) as cur:
            cur.execute(
                "INSERT INTO audit_log (tenant_id, actor_sub, actor_role, action,"
                " resource_type, resource_id, correlation_id, client_ip_masked)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    principal.tenant_id,
                    principal.subject,
                    principal.role.value,
                    action,
                    resource_type,
                    resource_id,
                    correlation_id,
                    client_ip_masked,
                ),
            )

    # ------------------------------------------------------------ idle events

    def list_idle_events(
        self,
        principal: Principal,
        *,
        page_size: int,
        cursor: Cursor | None = None,
        vin: str | None = None,
        excessive_only: bool = False,
        since: datetime | None = None,
    ) -> Page[IdleEventRow]:
        """List idle events, newest first, keyset-paginated.

        Ordered by ``(started_at DESC, idle_event_id DESC)``. The primary key is part of
        the ordering because many events share a timestamp: sorting on time alone is not
        a total order, and a cursor over ties would skip or repeat rows.
        """
        conditions = ["i.tenant_id = %(tenant_id)s"]
        params: dict[str, Any] = {"tenant_id": principal.tenant_id}

        if vin:
            conditions.append("i.vin = %(vin)s")
            params["vin"] = vin
        if excessive_only:
            conditions.append("i.excessive")
        if since:
            conditions.append("i.started_at >= %(since)s")
            params["since"] = since
        if cursor is not None:
            # The keyset predicate. Row comparison rather than OR-chained inequalities,
            # so PostgreSQL can satisfy it from the composite index directly.
            conditions.append("(i.started_at, i.idle_event_id) < (%(cursor_ts)s, %(cursor_id)s)")
            params["cursor_ts"] = cursor.sort_datetime
            params["cursor_id"] = cursor.row_id

        # Over-fetch by one: the extra row proves another page exists without a second
        # query or a COUNT(*), which on a partitioned table would be its own scan.
        params["limit"] = page_size + 1

        with self._scoped(principal) as cur:
            cur.execute(
                f"""
                SELECT i.idle_event_id, i.vin, i.started_at, i.ended_at,
                       i.duration_seconds, i.sample_count, i.max_gap_seconds,
                       i.excessive, i.pto_active_any, i.start_lat, i.start_lon,
                       i.fuel_litres_est, i.cost_est, i.currency, i.formula_version,
                       i.burn_rate_lph, i.price_id, i.idle_threshold_seconds,
                       i.fuel_litres_measured, p.burns_fuel
                FROM idle_events i
                JOIN vehicles v ON v.vin = i.vin
                JOIN powertrains p ON p.powertrain_code = v.powertrain_code
                WHERE {" AND ".join(conditions)}
                ORDER BY i.started_at DESC, i.idle_event_id DESC
                LIMIT %(limit)s
                """,  # noqa: S608 - conditions come from an allow-list, values are bound
                params,
            )
            rows = [self._to_idle_event(r, principal) for r in cur.fetchall()]

        return build_page(
            rows,
            page_size=page_size,
            cursor_of=lambda row: Cursor.from_row(row.started_at, row.idle_event_id),
        )

    @staticmethod
    def _to_idle_event(row: Row, principal: Principal) -> IdleEventRow:
        (
            idle_event_id,
            vin,
            started_at,
            ended_at,
            duration_seconds,
            sample_count,
            max_gap_seconds,
            excessive,
            pto_active_any,
            start_lat,
            start_lon,
            fuel_litres_est,
            cost_est,
            currency,
            formula_version,
            burn_rate_lph,
            price_id,
            idle_threshold_seconds,
            fuel_litres_measured,
            burns_fuel,
        ) = row

        # Coordinates are coarsened for roles that do not need precision, at the point
        # of access rather than in storage (CLAUDE.md S11).
        lat, lon = coarsen_location(
            float(start_lat) if start_lat is not None else None,
            float(start_lon) if start_lon is not None else None,
            precise=principal.may_see_precise_location,
        )

        return IdleEventRow(
            idle_event_id=int(idle_event_id),
            vin=str(vin).strip(),
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=float(duration_seconds),
            sample_count=int(sample_count),
            max_gap_seconds=float(max_gap_seconds),
            excessive=bool(excessive),
            pto_active_any=bool(pto_active_any),
            start_lat=lat,
            start_lon=lon,
            fuel_litres_est=float(fuel_litres_est) if fuel_litres_est is not None else None,
            cost_est=Decimal(cost_est) if cost_est is not None else None,
            currency=str(currency) if currency else None,
            formula_version=str(formula_version),
            burn_rate_lph=float(burn_rate_lph) if burn_rate_lph is not None else None,
            price_id=str(price_id) if price_id else None,
            idle_threshold_seconds=int(idle_threshold_seconds),
            fuel_litres_measured=(
                float(fuel_litres_measured) if fuel_litres_measured is not None else None
            ),
            # A BEV with no cost is an exclusion, not a missing value (D9).
            bev_excluded=not bool(burns_fuel),
        )

    # ---------------------------------------------------------- fleet summary

    def fleet_summary(
        self, principal: Principal, *, since: datetime, until: datetime
    ) -> FleetSummary:
        """Aggregate KPIs for the overview screen."""
        with self._scoped(principal) as cur:
            cur.execute(
                """
                WITH scoped AS (
                    SELECT i.*, p.burns_fuel
                    FROM idle_events i
                    JOIN vehicles v ON v.vin = i.vin
                    JOIN powertrains p ON p.powertrain_code = v.powertrain_code
                    WHERE i.tenant_id = %(tenant_id)s
                      AND i.started_at >= %(since)s
                      AND i.started_at < %(until)s
                )
                SELECT
                    (SELECT count(*) FROM vehicles WHERE tenant_id = %(tenant_id)s
                                                     AND is_active),
                    (SELECT count(DISTINCT vin) FROM scoped),
                    (SELECT count(*) FROM scoped),
                    (SELECT count(*) FROM scoped WHERE excessive),
                    (SELECT coalesce(sum(duration_seconds), 0) / 3600.0 FROM scoped),
                    (SELECT coalesce(sum(fuel_litres_est), 0) FROM scoped),
                    (SELECT coalesce(sum(cost_est), 0) FROM scoped),
                    (SELECT max(currency) FROM scoped WHERE currency IS NOT NULL),
                    -- Counted, not hidden: the UI says "excludes N BEVs" beside the
                    -- total, because a partial figure presented as complete is wrong.
                    (SELECT count(DISTINCT vin) FROM scoped WHERE NOT burns_fuel),
                    -- Restricted to PRICED rows. BEV segments carry the placeholder
                    -- 'unpriced', which sorts after 'idle-fuel-v1', so an unrestricted
                    -- max() reported the whole fleet figure as unpriced - misleading
                    -- about the very thing this field exists to disclose.
                    (SELECT max(formula_version) FROM scoped WHERE cost_est IS NOT NULL),
                    (SELECT max(idle_threshold_seconds) FROM scoped
                      WHERE cost_est IS NOT NULL),
                    (SELECT count(*) FROM alerts
                      WHERE tenant_id = %(tenant_id)s AND acknowledged_at IS NULL)
                """,
                {"tenant_id": principal.tenant_id, "since": since, "until": until},
            )
            row = cur.fetchone()

        if row is None:
            raise DependencyUnavailableError("fleet summary query returned nothing")

        (
            vehicle_count,
            vehicles_with_idle,
            idle_event_count,
            excessive_count,
            idle_hours,
            fuel_litres,
            total_cost,
            currency,
            excluded_bev,
            formula_version,
            threshold,
            open_alerts,
        ) = row

        from fleet_common.domain.fuel import FUEL_FORMULA_VERSION
        from fleet_common.settings import cost_settings, idle_settings

        return FleetSummary(
            tenant_id=principal.tenant_id,
            window_from=since,
            window_to=until,
            vehicle_count=int(vehicle_count or 0),
            vehicles_with_idle=int(vehicles_with_idle or 0),
            idle_event_count=int(idle_event_count or 0),
            excessive_count=int(excessive_count or 0),
            total_idle_hours=float(idle_hours or 0.0),
            total_fuel_litres=float(fuel_litres or 0.0),
            total_cost=Decimal(total_cost or 0),
            # Fall back to configuration when the window is empty, so an empty state
            # still shows which assumptions *would* apply rather than blanks.
            currency=str(currency) if currency else cost_settings().default_currency,
            excluded_bev_count=int(excluded_bev or 0),
            formula_version=str(formula_version) if formula_version else FUEL_FORMULA_VERSION,
            idle_threshold_seconds=int(threshold or idle_settings().idle_min_seconds),
            open_alert_count=int(open_alerts or 0),
        )

    # ------------------------------------------------------------ leaderboard

    def idle_cost_leaderboard(
        self, principal: Principal, *, since: datetime, limit: int
    ) -> list[LeaderboardRow]:
        """Top-K vehicles by estimated idle cost.

        Complexity: the aggregation is O(n log k) in the database's top-K, over rows
        already narrowed by the ``(tenant_id, started_at DESC)`` index. Only BEVs are
        absent, because they have no fuel cost to rank by.
        """
        with self._scoped(principal) as cur:
            cur.execute(
                """
                SELECT i.vin, v.registration, v.class_code, v.powertrain_code, f.name,
                       count(*), count(*) FILTER (WHERE i.excessive),
                       sum(i.duration_seconds) / 3600.0,
                       coalesce(sum(i.fuel_litres_est), 0),
                       coalesce(sum(i.cost_est), 0), max(i.currency)
                FROM idle_events i
                JOIN vehicles v ON v.vin = i.vin
                JOIN fleets f ON f.fleet_id = v.fleet_id
                WHERE i.tenant_id = %(tenant_id)s
                  AND i.started_at >= %(since)s
                  AND i.cost_est IS NOT NULL
                GROUP BY i.vin, v.registration, v.class_code, v.powertrain_code, f.name
                ORDER BY sum(i.cost_est) DESC
                LIMIT %(limit)s
                """,
                {"tenant_id": principal.tenant_id, "since": since, "limit": limit},
            )
            rows = cur.fetchall()

        return [
            LeaderboardRow(
                vin=str(vin).strip(),
                registration=str(registration),
                vehicle_class=str(vehicle_class),
                powertrain=str(powertrain),
                fleet_name=str(fleet_name),
                idle_event_count=int(count),
                excessive_count=int(excessive),
                total_idle_hours=float(hours),
                total_fuel_litres=float(litres),
                total_cost=Decimal(cost),
                currency=str(currency) if currency else "",
            )
            for (
                vin,
                registration,
                vehicle_class,
                powertrain,
                fleet_name,
                count,
                excessive,
                hours,
                litres,
                cost,
                currency,
            ) in rows
        ]

    # --------------------------------------------------------- vehicle detail

    def vehicle_detail(self, principal: Principal, vin: str) -> VehicleDetail:
        """Vehicle master data plus its idle totals.

        Raises:
            NotFoundError: when the VIN does not exist *for this tenant*. Another
                tenant's vehicle is reported as absent rather than forbidden, because
                "403" would confirm the VIN exists somewhere.
        """
        with self._scoped(principal) as cur:
            cur.execute(
                """
                SELECT v.vin, v.registration, v.tenant_id, f.name, v.class_code,
                       v.powertrain_code, v.fuel_type, v.model_year, v.is_active,
                       p.burns_fuel,
                       (SELECT count(*) FROM idle_events i WHERE i.vin = v.vin),
                       (SELECT coalesce(sum(i.duration_seconds), 0) / 3600.0
                          FROM idle_events i WHERE i.vin = v.vin),
                       (SELECT sum(i.cost_est) FROM idle_events i WHERE i.vin = v.vin),
                       (SELECT max(i.currency) FROM idle_events i WHERE i.vin = v.vin)
                FROM vehicles v
                JOIN fleets f ON f.fleet_id = v.fleet_id
                JOIN powertrains p ON p.powertrain_code = v.powertrain_code
                WHERE v.vin = %(vin)s
                """,
                {"vin": vin},
            )
            row = cur.fetchone()

        if row is None:
            raise NotFoundError(f"no vehicle {vin!r} in this tenant")

        (
            found_vin,
            registration,
            tenant_id,
            fleet_name,
            vehicle_class,
            powertrain,
            fuel_type,
            model_year,
            is_active,
            burns_fuel,
            idle_count,
            idle_hours,
            total_cost,
            currency,
        ) = row

        return VehicleDetail(
            vin=str(found_vin).strip(),
            registration=str(registration),
            tenant_id=str(tenant_id),
            fleet_name=str(fleet_name),
            vehicle_class=str(vehicle_class),
            powertrain=str(powertrain),
            fuel_type=str(fuel_type) if fuel_type else None,
            model_year=int(model_year),
            is_active=bool(is_active),
            burns_fuel=bool(burns_fuel),
            idle_event_count=int(idle_count or 0),
            total_idle_hours=float(idle_hours or 0.0),
            total_cost=Decimal(total_cost) if total_cost is not None else None,
            currency=str(currency) if currency else None,
        )

    def daily_idle_trend(
        self, principal: Principal, *, since: date, until: date
    ) -> list[tuple[date, float, Decimal]]:
        """Daily idle hours and cost, for the overview trend chart."""
        with self._scoped(principal) as cur:
            cur.execute(
                """
                SELECT started_at::date AS day,
                       sum(duration_seconds) / 3600.0,
                       coalesce(sum(cost_est), 0)
                FROM idle_events
                WHERE tenant_id = %(tenant_id)s
                  AND started_at >= %(since)s
                  AND started_at < %(until)s
                GROUP BY day
                ORDER BY day
                """,
                {"tenant_id": principal.tenant_id, "since": since, "until": until},
            )
            return [(day, float(hours), Decimal(cost)) for day, hours, cost in cur.fetchall()]
