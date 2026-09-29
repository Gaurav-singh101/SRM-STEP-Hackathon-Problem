"""Daily rollups and utilisation scoring (D3, D6).

Why rollups exist at all: the fleet overview and the leaderboard aggregate idle cost
across whole fleets over 30-day windows. Computing that from raw ``idle_events`` on
every page load is an unbounded scan whose cost grows with history, which the API's
p95 < 200 ms target cannot absorb. Rollups turn it into an indexed lookup.

Two things keep them trustworthy:

*They are derived, never authoritative.* ``idle_events`` and ``trips`` remain the source
of truth, and any rollup key can be rebuilt from them.

*They are recomputed per key, not refreshed wholesale.* A late event invalidates exactly
one (vehicle, day), so reconciliation recomputes that one key rather than rebuilding a
fleet's history - which is what makes late-event handling affordable (D6).

The utilisation scoring itself is not implemented here. It lives in
``fleet_common.domain.utilisation`` as a pure function with its own tests; this module
only feeds it rows and stores what it returns (CLAUDE.md S7).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

import psycopg
import psycopg.rows
from psycopg.rows import TupleRow

from fleet_common.domain.utilisation import (
    UTILISATION_FORMULA_VERSION,
    ClassBaseline,
    UtilisationConfig,
    VehicleActivity,
    build_class_baseline,
    score_utilisation,
)

logger = logging.getLogger(__name__)

#: Window for the utilisation score (D3). 30 days matches a monthly finance cycle and
#: is long enough that a single quiet week does not condemn a vehicle.
DEFAULT_UTILISATION_WINDOW_DAYS: Final[int] = 30

#: Minimum peers before a class baseline is considered usable. Below this, MAD
#: describes noise rather than spread (ADR-0003).
MIN_BASELINE_SAMPLE: Final[int] = 5


@dataclass(frozen=True, slots=True)
class RollupCounts:
    """What one rollup run wrote."""

    vehicle_days: int = 0
    fleet_days: int = 0
    utilisation_scores: int = 0
    baselines: int = 0
    skipped_classes: int = 0
    #: Vehicles deliberately left unscored because no telemetry reached us for them.
    no_data_vehicles: int = 0


def _set_role_batch(cur: psycopg.Cursor[TupleRow]) -> None:
    """Switch to the maintenance role.

    Rollups genuinely cross tenants - they rebuild every tenant's aggregates - so this
    uses the BYPASSRLS role rather than looping tenant contexts. That is exactly why
    ``fleet_batch`` exists as a separate, named role: "this code sees every tenant" is
    then a visible property of the deployment rather than an accident (ADR-0006).
    """
    cur.execute("SET ROLE fleet_batch")


def compute_vehicle_daily(
    conn: psycopg.Connection[TupleRow], *, day: date, tenant_id: str | None = None
) -> int:
    """Rebuild ``vehicle_daily`` for one calendar day.

    Idempotent: an upsert keyed on (tenant, vin, day), so re-running after a late event
    corrects the row rather than duplicating it.

    The day boundary uses the tenant's local timezone, not UTC. "Which day did this cost
    fall on" is a local-time question and billing periods are local, so bucketing by UTC
    would mis-assign every evening event for a tenant east of Greenwich.
    """
    with conn.cursor() as cur:
        _set_role_batch(cur)
        cur.execute(
            """
            INSERT INTO vehicle_daily (
                tenant_id, vin, day,
                idle_seconds, idle_event_count, excessive_count,
                fuel_litres_est, cost_est, currency,
                moving_seconds, distance_km, was_active,
                formula_version, recomputed_at
            )
            SELECT
                v.tenant_id,
                v.vin,
                %(day)s::date,
                coalesce(i.idle_seconds, 0),
                coalesce(i.idle_event_count, 0),
                coalesce(i.excessive_count, 0),
                coalesce(i.fuel_litres_est, 0),
                coalesce(i.cost_est, 0),
                i.currency,
                coalesce(t.moving_seconds, 0),
                coalesce(t.distance_km, 0),
                coalesce(t.trip_count, 0) > 0,
                coalesce(i.formula_version, 'none'),
                now()
            FROM vehicles v
            -- Joined before the lateral subqueries, because a LATERAL can only
            -- reference tables to its left. The tenant timezone is what makes the day
            -- boundary local rather than UTC.
            JOIN tenants tn ON tn.tenant_id = v.tenant_id
            LEFT JOIN LATERAL (
                SELECT
                    sum(duration_seconds) AS idle_seconds,
                    count(*) AS idle_event_count,
                    count(*) FILTER (WHERE excessive) AS excessive_count,
                    sum(fuel_litres_est) AS fuel_litres_est,
                    sum(cost_est) AS cost_est,
                    max(currency) AS currency,
                    max(formula_version) FILTER (WHERE cost_est IS NOT NULL)
                        AS formula_version
                FROM idle_events e
                WHERE e.vin = v.vin
                  AND (e.started_at AT TIME ZONE tn.timezone)::date = %(day)s::date
            ) i ON true
            LEFT JOIN LATERAL (
                SELECT
                    count(*) AS trip_count,
                    sum(moving_seconds) AS moving_seconds,
                    sum(distance_km) AS distance_km
                FROM trips p
                WHERE p.vin = v.vin
                  AND (p.started_at AT TIME ZONE tn.timezone)::date = %(day)s::date
            ) t ON true
            WHERE v.is_active
              AND (%(tenant_id)s::text IS NULL OR v.tenant_id = %(tenant_id)s)
              -- A vehicle with no activity at all still gets a row: an absent row is
              -- indistinguishable from a vehicle that was never registered, and the
              -- active-day ratio needs the zero to count against it (D3).
            ON CONFLICT (tenant_id, vin, day) DO UPDATE SET
                idle_seconds = EXCLUDED.idle_seconds,
                idle_event_count = EXCLUDED.idle_event_count,
                excessive_count = EXCLUDED.excessive_count,
                fuel_litres_est = EXCLUDED.fuel_litres_est,
                cost_est = EXCLUDED.cost_est,
                currency = EXCLUDED.currency,
                moving_seconds = EXCLUDED.moving_seconds,
                distance_km = EXCLUDED.distance_km,
                was_active = EXCLUDED.was_active,
                formula_version = EXCLUDED.formula_version,
                recomputed_at = now()
            """,
            {"day": day, "tenant_id": tenant_id},
        )
        written = cur.rowcount
    conn.commit()
    return max(written, 0)


def compute_fleet_daily(
    conn: psycopg.Connection[TupleRow], *, day: date, tenant_id: str | None = None
) -> int:
    """Roll ``vehicle_daily`` up to ``fleet_daily`` for one day.

    Built from the vehicle rollup rather than from raw events: one derivation step, so
    the two cannot disagree about the same day.
    """
    with conn.cursor() as cur:
        _set_role_batch(cur)
        cur.execute(
            """
            INSERT INTO fleet_daily (
                tenant_id, fleet_id, day,
                idle_seconds, idle_event_count, excessive_count,
                fuel_litres_est, cost_est, currency,
                vehicle_count, active_vehicle_count, excluded_bev_count,
                formula_version, recomputed_at
            )
            SELECT
                d.tenant_id,
                v.fleet_id,
                d.day,
                sum(d.idle_seconds),
                sum(d.idle_event_count),
                sum(d.excessive_count),
                sum(d.fuel_litres_est),
                sum(d.cost_est),
                max(d.currency),
                count(*),
                count(*) FILTER (WHERE d.was_active),
                -- Counted, not hidden: the UI states "excludes N BEVs" beside the
                -- total, because a partial figure presented as complete is wrong (D9).
                count(*) FILTER (WHERE NOT p.burns_fuel AND d.idle_event_count > 0),
                coalesce(max(d.formula_version) FILTER (WHERE d.cost_est > 0), 'none'),
                now()
            FROM vehicle_daily d
            JOIN vehicles v ON v.vin = d.vin
            JOIN powertrains p ON p.powertrain_code = v.powertrain_code
            WHERE d.day = %(day)s::date
              AND (%(tenant_id)s::text IS NULL OR d.tenant_id = %(tenant_id)s)
            GROUP BY d.tenant_id, v.fleet_id, d.day
            ON CONFLICT (tenant_id, fleet_id, day) DO UPDATE SET
                idle_seconds = EXCLUDED.idle_seconds,
                idle_event_count = EXCLUDED.idle_event_count,
                excessive_count = EXCLUDED.excessive_count,
                fuel_litres_est = EXCLUDED.fuel_litres_est,
                cost_est = EXCLUDED.cost_est,
                currency = EXCLUDED.currency,
                vehicle_count = EXCLUDED.vehicle_count,
                active_vehicle_count = EXCLUDED.active_vehicle_count,
                excluded_bev_count = EXCLUDED.excluded_bev_count,
                formula_version = EXCLUDED.formula_version,
                recomputed_at = now()
            """,
            {"day": day, "tenant_id": tenant_id},
        )
        written = cur.rowcount
    conn.commit()
    return max(written, 0)


def load_window_activity(
    conn: psycopg.Connection[TupleRow],
    *,
    tenant_id: str,
    window_end: date,
    window_days: int,
) -> tuple[dict[str, list[VehicleActivity]], int]:
    """Aggregate ``vehicle_daily`` into one ``VehicleActivity`` per vehicle.

    Returns activities grouped by vehicle class, plus the number of vehicles excluded
    for having no telemetry at all.

    Grouped by class because utilisation is scored against class peers: judging a heavy
    truck by a city car's norms would be meaningless.

    **Vehicles with no observed telemetry in the window are excluded, not scored zero.**
    A vehicle whose telematics unit has failed sends nothing, and scoring it as unused
    would tell a fleet manager to sell a van that is working perfectly well. "We have no
    data" and "this asset is idle" are different answers, and only the second is
    actionable, so they must not be collapsed into one band.

    KNOWN LIMITATION: this cannot yet distinguish "no telemetry received" from
    "reporting normally but parked with the engine off all window", because both produce
    no trips and no idle events. Raw samples live in MongoDB under a 48h TTL, so they are
    not available across a 30-day window. Treating both as unknown is the conservative
    choice; a per-vehicle-day sample count written by the processor would separate them.
    """
    window_start = window_end - timedelta(days=window_days - 1)

    with conn.cursor() as cur:
        _set_role_batch(cur)
        cur.execute(
            """
            SELECT v.class_code, d.vin,
                   count(*) FILTER (WHERE d.was_active),
                   sum(d.moving_seconds) / 3600.0,
                   sum(d.distance_km),
                   sum(d.idle_seconds) / 3600.0,
                   -- Any sign at all that telemetry reached us for this vehicle.
                   (sum(d.moving_seconds) + sum(d.idle_seconds)
                      + sum(d.idle_event_count) + sum(d.distance_km)) > 0 AS has_data
            FROM vehicle_daily d
            JOIN vehicles v ON v.vin = d.vin
            WHERE d.tenant_id = %(tenant_id)s
              AND d.day BETWEEN %(start)s AND %(end)s
              AND v.is_active
            GROUP BY v.class_code, d.vin
            """,
            {"tenant_id": tenant_id, "start": window_start, "end": window_end},
        )
        rows = cur.fetchall()

    by_class: dict[str, list[VehicleActivity]] = {}
    no_data = 0
    for class_code, vin, active_days, moving_hours, distance_km, idle_hours, has_data in rows:
        if not has_data:
            no_data += 1
            continue
        activity = VehicleActivity(
            vin=str(vin).strip(),
            window_days=window_days,
            active_days=int(active_days or 0),
            moving_hours=float(moving_hours or 0.0),
            distance_km=float(distance_km or 0.0),
            idle_hours=float(idle_hours or 0.0),
        )
        by_class.setdefault(str(class_code), []).append(activity)
    return by_class, no_data


def compute_utilisation(
    conn: psycopg.Connection[TupleRow],
    *,
    tenant_id: str,
    window_end: date,
    window_days: int = DEFAULT_UTILISATION_WINDOW_DAYS,
    config: UtilisationConfig | None = None,
) -> tuple[int, int, int, int]:
    """Score every vehicle's utilisation for one window.

    Returns (scores written, baselines written, classes skipped, vehicles with no data).

    A class with too few peers is **skipped, not scored with a weak baseline**. A score
    computed against three peers would look identical to one computed against three
    hundred, and the second is worth acting on while the first is not.
    """
    cfg = config or UtilisationConfig()
    by_class, no_data = load_window_activity(
        conn, tenant_id=tenant_id, window_end=window_end, window_days=window_days
    )

    # Clear this window's scores first. Without it a vehicle that stops reporting keeps
    # whatever score it last had, and a stale score looks identical to a current one -
    # which is worse than no score, because someone would act on it.
    _clear_window_scores(conn, tenant_id=tenant_id, window_end=window_end)

    scored = 0
    baselines_written = 0
    skipped = 0

    for class_code, activities in by_class.items():
        if len(activities) < MIN_BASELINE_SAMPLE:
            logger.info(
                "skipping utilisation for class %s in %s: only %d peers, below the "
                "minimum of %d for a usable baseline",
                class_code,
                tenant_id,
                len(activities),
                MIN_BASELINE_SAMPLE,
            )
            skipped += 1
            continue

        baseline = build_class_baseline(class_code, activities)
        _store_baseline(
            conn,
            tenant_id=tenant_id,
            baseline=baseline,
            window_end=window_end,
            window_days=window_days,
        )
        baselines_written += 1

        scores = [score_utilisation(a, baseline, cfg) for a in activities]
        scored += _store_scores(
            conn,
            tenant_id=tenant_id,
            scores=scores,
            baseline=baseline,
            window_end=window_end,
        )

    return scored, baselines_written, skipped, no_data


def _clear_window_scores(
    conn: psycopg.Connection[TupleRow], *, tenant_id: str, window_end: date
) -> int:
    """Remove existing utilisation scores for one tenant and window.

    Rollups recompute a whole window at once, so clearing first is what makes the result
    reflect current data rather than accumulating whatever any previous run left behind.
    """
    with conn.cursor() as cur:
        _set_role_batch(cur)
        cur.execute(
            "DELETE FROM vehicle_utilisation WHERE tenant_id = %s AND window_end = %s",
            (tenant_id, window_end),
        )
        removed = cur.rowcount
    conn.commit()
    return max(removed, 0)


def _store_baseline(
    conn: psycopg.Connection[TupleRow],
    *,
    tenant_id: str,
    baseline: ClassBaseline,
    window_end: date,
    window_days: int,
) -> None:
    """Persist the baseline a score was computed against.

    Stored so a score can be *explained* after the fact - "measured against these peers,
    this median" - rather than only recomputed, which would give a different answer once
    the underlying data moved on.
    """
    with conn.cursor() as cur:
        _set_role_batch(cur)
        cur.execute(
            """
            INSERT INTO class_baselines (
                tenant_id, class_code, window_end, window_days,
                median_moving_hours_per_day, median_distance_km_per_day,
                median_active_day_ratio, mad_moving_hours_per_day,
                mad_distance_km_per_day, mad_active_day_ratio,
                sample_size, formula_version, recomputed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (tenant_id, class_code, window_end) DO UPDATE SET
                window_days = EXCLUDED.window_days,
                median_moving_hours_per_day = EXCLUDED.median_moving_hours_per_day,
                median_distance_km_per_day = EXCLUDED.median_distance_km_per_day,
                median_active_day_ratio = EXCLUDED.median_active_day_ratio,
                mad_moving_hours_per_day = EXCLUDED.mad_moving_hours_per_day,
                mad_distance_km_per_day = EXCLUDED.mad_distance_km_per_day,
                mad_active_day_ratio = EXCLUDED.mad_active_day_ratio,
                sample_size = EXCLUDED.sample_size,
                formula_version = EXCLUDED.formula_version,
                recomputed_at = now()
            """,
            (
                tenant_id,
                baseline.vehicle_class,
                window_end,
                window_days,
                baseline.median_moving_hours_per_day,
                baseline.median_distance_km_per_day,
                baseline.median_active_day_ratio,
                baseline.mad_moving_hours_per_day,
                baseline.mad_distance_km_per_day,
                baseline.mad_active_day_ratio,
                baseline.sample_size,
                UTILISATION_FORMULA_VERSION,
            ),
        )
    conn.commit()


def _store_scores(
    conn: psycopg.Connection[TupleRow],
    *,
    tenant_id: str,
    scores: Sequence[Any],
    baseline: ClassBaseline,
    window_end: date,
) -> int:
    with conn.cursor() as cur:
        _set_role_batch(cur)
        cur.executemany(
            """
            INSERT INTO vehicle_utilisation (
                tenant_id, vin, window_end, window_days,
                score, band, active_day_component, moving_hours_component,
                distance_component, idle_share, max_abs_z, component_spread,
                abnormal_reason, baseline_sample_size, formula_version, recomputed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (tenant_id, vin, window_end) DO UPDATE SET
                window_days = EXCLUDED.window_days,
                score = EXCLUDED.score,
                band = EXCLUDED.band,
                active_day_component = EXCLUDED.active_day_component,
                moving_hours_component = EXCLUDED.moving_hours_component,
                distance_component = EXCLUDED.distance_component,
                idle_share = EXCLUDED.idle_share,
                max_abs_z = EXCLUDED.max_abs_z,
                component_spread = EXCLUDED.component_spread,
                abnormal_reason = EXCLUDED.abnormal_reason,
                baseline_sample_size = EXCLUDED.baseline_sample_size,
                formula_version = EXCLUDED.formula_version,
                recomputed_at = now()
            """,
            [
                (
                    tenant_id,
                    s.vin,
                    window_end,
                    s.window_days,
                    s.score,
                    s.band.value,
                    s.active_day_component,
                    s.moving_hours_component,
                    s.distance_component,
                    s.idle_share,
                    s.max_abs_z,
                    s.component_spread,
                    s.abnormal_reason,
                    baseline.sample_size,
                    s.formula_version,
                )
                for s in scores
            ],
        )
        written = cur.rowcount
    conn.commit()
    return max(written, 0)


def affected_days_for(
    started_at: datetime, timezone_name: str, *, lookback_days: int = 1
) -> list[date]:
    """Local calendar days a late event could affect.

    More than one, because an event near local midnight can change the day it falls on
    *and* the neighbouring one once the day boundary is applied - and because an idle
    segment can span midnight. Recomputing a spare day is cheap; missing one leaves a
    silently stale rollup (D6).
    """
    del timezone_name  # The SQL applies the tenant timezone; this is the UTC envelope.
    base = started_at.date()
    return [base - timedelta(days=offset) for offset in range(lookback_days + 1)]
