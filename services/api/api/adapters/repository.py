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
class UtilisationSnapshot:
    """One vehicle's utilisation score from the latest window.

    A cut-down view of ``UtilisationRow``: the detail page needs the score and the reason
    behind it, not the fleet-ranking fields. Kept as its own type rather than reusing the
    row so the vehicle endpoint does not grow a dependency on the leaderboard's shape.
    """

    window_end: date
    window_days: int
    score: float
    band: str
    active_day_component: float
    moving_hours_component: float
    distance_component: float
    idle_share: float
    abnormal_reason: str | None
    baseline_sample_size: int
    formula_version: str


@dataclass(frozen=True, slots=True)
class Leaderboard:
    """A leaderboard page plus how current it is.

    The freshness fields are not decoration. The ranking is built from a rollup the batch
    job maintains, so a leaderboard can be hours old, and one presented without saying so
    would have a finance manager acting on yesterday's fleet as if it were today's.
    ``None`` on both when the rollup is empty - which is "not computed yet", not "zero
    cost".
    """

    items: list[LeaderboardRow]
    data_through: date | None
    computed_at: datetime | None


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
    #: ``None`` when this vehicle has no score in the latest window. Not a zero score:
    #: "we could not score it" and "it scored badly" lead to opposite actions.
    utilisation: UtilisationSnapshot | None = None


@dataclass(frozen=True, slots=True)
class UtilisationRow:
    """One vehicle's utilisation score with the components behind it."""

    vin: str
    registration: str
    vehicle_class: str
    powertrain: str
    fleet_name: str
    window_end: date
    window_days: int
    score: float
    band: str
    active_day_component: float
    moving_hours_component: float
    distance_component: float
    idle_share: float
    #: Gap between the highest and lowest component; this is what decides ABNORMAL.
    component_spread: float
    abnormal_reason: str | None
    #: Peers the baseline was built from. A score from 5 peers is far weaker than one
    #: from 500, and the UI says so rather than presenting both identically.
    baseline_sample_size: int
    formula_version: str


@dataclass(frozen=True, slots=True)
class UtilisationWindowRow:
    """How much of the fleet the latest utilisation window could score.

    Read rather than derived. "Active vehicles without a score row" would collapse two
    different causes - no telemetry, and a class with too few peers - into one number
    that answers neither question, so the batch job records what it observed.
    """

    window_end: date
    window_days: int
    vehicles_active: int
    scored: int
    unscored_no_telemetry: int
    unscored_class_too_small: int
    formula_version: str


@dataclass(frozen=True, slots=True)
class AlertRow:
    """One alert needing attention."""

    alert_id: int
    vin: str
    registration: str
    alert_type: str
    severity: str
    triggering_event_ts: datetime
    ingest_ts: datetime | None
    persisted_at: datetime
    idle_event_started_at: datetime | None
    detail: dict[str, Any]
    acknowledged_at: datetime | None

    @property
    def end_to_end_latency_seconds(self) -> float:
        """From the event happening to the alert being durable - the D10 SLO.

        Meaningful for live streaming only. On replayed history it measures how old the
        data was rather than how fast the system reacted: backfilling ten days of
        telemetry produced figures around 83,000 seconds.
        """
        return (self.persisted_at - self.triggering_event_ts).total_seconds()

    @property
    def processing_latency_seconds(self) -> float | None:
        """From the processor receiving the event to the alert being durable.

        Meaningful whatever the age of the data, which makes it the honest figure to
        quote for a backfill. ``None`` for alerts written before ingest_ts existed.
        """
        if self.ingest_ts is None:
            return None
        return (self.persisted_at - self.ingest_ts).total_seconds()

    @property
    def is_backfill(self) -> bool:
        """Whether this alert came from replayed history rather than live telemetry.

        Surfaced so a latency figure is never read without the context that decides
        whether it means anything. A minute is far beyond any live pipeline delay.
        """
        if self.ingest_ts is None:
            return False
        return (self.ingest_ts - self.triggering_event_ts).total_seconds() > 60


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
    ) -> Leaderboard:
        """Top-K vehicles by estimated idle cost, read from the daily rollup.

        **From ``vehicle_daily``, not from ``idle_events``.** The obvious implementation
        aggregates raw events at request time, and that is what this did until it was
        measured. Over a 30-day window one tenant's events are a large fraction of the
        table, so the planner correctly chooses a sequential scan and the index does almost
        nothing: 1.15x at two million rows. No index fixes a query that has to read most of
        the rows.

        Complexity: O(d x v) rows scanned, where d is days in the window and v is vehicles
        with activity, then a top-K sort. The point is not the constant factor but what the
        cost grows with - **vehicle-days rather than events**. Raw aggregation gets more
        expensive as telemetry volume rises; this does not.

        Measured at two million events, comparing both variants as they ship: this path is
        about **3x faster** than raw aggregation with non-overlapping ranges across two
        independent nine-sample runs, and it lands **under p95 < 200 ms** where the raw path
        does not. The gain is 3x rather than an order of magnitude because compression is
        only about 2:1 at that event density - which is why the scaling argument above
        matters more than the constant factor.

        If the target becomes tight at fleet scale the next step is a coarser pre-aggregate
        keyed on ``(tenant_id, window_end, vin)`` with the window totals already summed, so
        the request becomes an index range scan plus a top-K over one row per vehicle rather
        than a ``GROUP BY`` over thirty. Recorded in ``docs/evidence/sql/query-plans.md``;
        not built.

        The cost of reading a rollup is freshness, so the result carries ``data_through``
        and ``computed_at`` rather than letting a stale leaderboard pass for a live one.
        Verified against the demo data to agree with the raw aggregation exactly: 92
        vehicles and 38,085.70 total either way, no vehicle differing.
        """
        with self._scoped(principal) as cur:
            cur.execute(
                """
                SELECT d.vin, v.registration, v.class_code, v.powertrain_code, f.name,
                       sum(d.idle_event_count), sum(d.excessive_count),
                       sum(d.idle_seconds) / 3600.0,
                       sum(d.fuel_litres_est),
                       sum(d.cost_est), max(d.currency),
                       max(d.day), max(d.recomputed_at)
                FROM vehicle_daily d
                JOIN vehicles v ON v.vin = d.vin
                JOIN fleets f ON f.fleet_id = v.fleet_id
                WHERE d.tenant_id = %(tenant_id)s
                  AND d.day >= %(since)s
                  -- Excludes BEVs without a special case: a vehicle with no fuel cost
                  -- contributes 0, and a leaderboard of zeros is not worth ranking.
                  AND d.cost_est > 0
                GROUP BY d.vin, v.registration, v.class_code, v.powertrain_code, f.name
                ORDER BY sum(d.cost_est) DESC
                LIMIT %(limit)s
                """,
                {"tenant_id": principal.tenant_id, "since": since.date(), "limit": limit},
            )
            rows = cur.fetchall()

        entries = [
            LeaderboardRow(
                vin=str(vin).strip(),
                registration=str(registration),
                vehicle_class=str(vehicle_class),
                powertrain=str(powertrain),
                fleet_name=str(fleet_name),
                idle_event_count=int(count),
                excessive_count=int(excessive),
                total_idle_hours=float(hours),
                total_fuel_litres=float(litres or 0.0),
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
                _day,
                _recomputed,
            ) in rows
        ]

        # Freshness from the rows themselves rather than a separate query: whatever the
        # leaderboard is built from is what it is current as of, by construction.
        return Leaderboard(
            items=entries,
            data_through=max((row[11] for row in rows), default=None),
            computed_at=max((row[12] for row in rows), default=None),
        )

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
                       (SELECT max(i.currency) FROM idle_events i WHERE i.vin = v.vin),
                       u.window_end, u.window_days, u.score, u.band,
                       u.active_day_component, u.moving_hours_component,
                       u.distance_component, u.idle_share, u.abnormal_reason,
                       u.baseline_sample_size, u.formula_version
                FROM vehicles v
                JOIN fleets f ON f.fleet_id = v.fleet_id
                JOIN powertrains p ON p.powertrain_code = v.powertrain_code
                -- LEFT JOIN, and only the newest window: an unscored vehicle must still
                -- return its master data and idle cost. Restricting the window inside the
                -- join rather than in WHERE is what keeps that true - a WHERE clause on
                -- u.window_end would turn this back into an inner join.
                LEFT JOIN vehicle_utilisation u
                       ON u.vin = v.vin
                      AND u.tenant_id = v.tenant_id
                      AND u.window_end = (SELECT max(window_end) FROM vehicle_utilisation
                                           WHERE tenant_id = v.tenant_id)
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
            u_window_end,
            u_window_days,
            u_score,
            u_band,
            u_active,
            u_moving,
            u_distance,
            u_idle_share,
            u_abnormal_reason,
            u_baseline_size,
            u_formula_version,
        ) = row

        # Built only when a score exists. A snapshot of zeros would be read as a score.
        snapshot = (
            UtilisationSnapshot(
                window_end=u_window_end,
                window_days=int(u_window_days),
                score=float(u_score),
                band=str(u_band),
                active_day_component=float(u_active),
                moving_hours_component=float(u_moving),
                distance_component=float(u_distance),
                idle_share=float(u_idle_share),
                abnormal_reason=str(u_abnormal_reason) if u_abnormal_reason else None,
                baseline_sample_size=int(u_baseline_size),
                formula_version=str(u_formula_version),
            )
            if u_window_end is not None
            else None
        )

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
            utilisation=snapshot,
        )

    # ------------------------------------------------------------ utilisation

    def list_utilisation(
        self,
        principal: Principal,
        *,
        page_size: int,
        cursor: Cursor | None = None,
        bands: tuple[str, ...] = (),
    ) -> Page[UtilisationRow]:
        """Utilisation scores for the most recent window, worst first.

        Ordered ascending by score, because the point of the view is the vehicles at the
        bottom: they belong on the first page, not the last.
        """
        conditions = [
            "u.tenant_id = %(tenant_id)s",
            # Newest window only. Older rows are kept for history, but showing several
            # windows together would mix scores computed over different data.
            "u.window_end = (SELECT max(window_end) FROM vehicle_utilisation"
            "                 WHERE tenant_id = %(tenant_id)s)",
        ]
        params: dict[str, Any] = {"tenant_id": principal.tenant_id}

        if bands:
            # Bound to an allow-listed set by the router, never raw caller input.
            conditions.append("u.band = ANY(%(bands)s)")
            params["bands"] = list(bands)
        if cursor is not None:
            # Score ascending, VIN breaking ties: a total order, so a cursor cannot
            # land ambiguously between two vehicles with the same score.
            conditions.append("(u.score, u.vin) > (%(cursor_score)s, %(cursor_vin)s)")
            params["cursor_score"] = float(cursor.sort_value)
            params["cursor_vin"] = cursor.tie_break

        params["limit"] = page_size + 1

        with self._scoped(principal) as cur:
            cur.execute(
                f"""
                SELECT u.vin, v.registration, v.class_code, v.powertrain_code, f.name,
                       u.window_end, u.window_days, u.score, u.band,
                       u.active_day_component, u.moving_hours_component,
                       u.distance_component, u.idle_share, u.component_spread,
                       u.abnormal_reason, u.baseline_sample_size, u.formula_version
                FROM vehicle_utilisation u
                JOIN vehicles v ON v.vin = u.vin
                JOIN fleets f ON f.fleet_id = v.fleet_id
                WHERE {" AND ".join(conditions)}
                ORDER BY u.score ASC, u.vin ASC
                LIMIT %(limit)s
                """,  # noqa: S608 - conditions are an allow-list, values are bound
                params,
            )
            rows = [self._to_utilisation(r) for r in cur.fetchall()]

        return build_page(
            rows,
            page_size=page_size,
            cursor_of=lambda row: Cursor.with_tie_break(str(row.score), row.vin),
        )

    @staticmethod
    def _to_utilisation(row: Row) -> UtilisationRow:
        (
            vin,
            registration,
            vehicle_class,
            powertrain,
            fleet_name,
            window_end,
            window_days,
            score,
            band,
            active_c,
            moving_c,
            distance_c,
            idle_share,
            component_spread,
            abnormal_reason,
            baseline_sample_size,
            formula_version,
        ) = row
        return UtilisationRow(
            vin=str(vin).strip(),
            registration=str(registration),
            vehicle_class=str(vehicle_class),
            powertrain=str(powertrain),
            fleet_name=str(fleet_name),
            window_end=window_end,
            window_days=int(window_days),
            score=float(score),
            band=str(band),
            active_day_component=float(active_c),
            moving_hours_component=float(moving_c),
            distance_component=float(distance_c),
            idle_share=float(idle_share),
            component_spread=float(component_spread),
            abnormal_reason=str(abnormal_reason) if abnormal_reason else None,
            baseline_sample_size=int(baseline_sample_size),
            formula_version=str(formula_version),
        )

    def utilisation_coverage(self, principal: Principal) -> UtilisationWindowRow | None:
        """Accounting for the most recent scored window, or ``None`` if none exists.

        ``None`` rather than a row of zeros: "no window has been scored yet" and "every
        vehicle was scored" are different statements, and the second one shown in place
        of the first is the exact misreading this table was added to prevent.
        """
        with self._scoped(principal) as cur:
            cur.execute(
                """
                SELECT window_end, window_days, vehicles_active, scored,
                       unscored_no_telemetry, unscored_class_too_small, formula_version
                FROM utilisation_window
                WHERE tenant_id = %(tenant_id)s
                ORDER BY window_end DESC
                LIMIT 1
                """,
                {"tenant_id": principal.tenant_id},
            )
            row = cur.fetchone()

        if row is None:
            return None
        return UtilisationWindowRow(
            window_end=row[0],
            window_days=int(row[1]),
            vehicles_active=int(row[2]),
            scored=int(row[3]),
            unscored_no_telemetry=int(row[4]),
            unscored_class_too_small=int(row[5]),
            formula_version=str(row[6]),
        )

    # ----------------------------------------------------------------- alerts

    def list_alerts(
        self,
        principal: Principal,
        *,
        limit: int,
        unacknowledged_only: bool = True,
    ) -> list[AlertRow]:
        """Alerts needing attention, newest first."""
        conditions = ["a.tenant_id = %(tenant_id)s"]
        params: dict[str, Any] = {"tenant_id": principal.tenant_id, "limit": limit}
        if unacknowledged_only:
            conditions.append("a.acknowledged_at IS NULL")

        with self._scoped(principal) as cur:
            cur.execute(
                f"""
                SELECT a.alert_id, a.vin, v.registration, a.alert_type, a.severity,
                       a.triggering_event_ts, a.ingest_ts, a.persisted_at,
                       a.idle_event_started_at, a.detail, a.acknowledged_at
                FROM alerts a
                JOIN vehicles v ON v.vin = a.vin
                WHERE {" AND ".join(conditions)}
                ORDER BY a.triggering_event_ts DESC
                LIMIT %(limit)s
                """,  # noqa: S608 - conditions are an allow-list, values are bound
                params,
            )
            rows = cur.fetchall()

        return [
            AlertRow(
                alert_id=int(alert_id),
                vin=str(vin).strip(),
                registration=str(registration),
                alert_type=str(alert_type),
                severity=str(severity),
                triggering_event_ts=triggering_ts,
                ingest_ts=ingest_ts,
                persisted_at=persisted_at,
                idle_event_started_at=segment_start,
                detail=dict(detail) if detail else {},
                acknowledged_at=acknowledged_at,
            )
            for (
                alert_id,
                vin,
                registration,
                alert_type,
                severity,
                triggering_ts,
                ingest_ts,
                persisted_at,
                segment_start,
                detail,
                acknowledged_at,
            ) in rows
        ]

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
