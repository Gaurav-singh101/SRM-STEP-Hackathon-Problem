"""Persistence adapters: Postgres, MongoDB and Redis.

Every write here is **batched**. Per-event round trips are forbidden in the hot path
(CLAUDE.md S3), and at 100K events/s the difference is not a matter of degree: a
round trip per event caps throughput at the network latency, however fast the stores
are.

Every write is also **idempotent**. Delivery is at-least-once, so a batch can be
replayed after a commit failure, and the sinks must absorb that without
double-counting. Postgres uses ``ON CONFLICT DO NOTHING`` against the unique index
that ADR-0004 designates the final dedup guarantee; Mongo upserts by ``event_id``;
Redis writes are last-writer-wins by nature.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import orjson
import psycopg
import psycopg.rows
from psycopg_pool import ConnectionPool
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError
from redis import Redis

from fleet_common.events import Powertrain, TelemetryEvent
from fleet_common.settings import (
    MongoSettings,
    PostgresSettings,
    RedisSettings,
)
from processor.pipeline import Alert, LatestState, PricedIdleSegment, VehicleRef
from processor.validation import Rejection

logger = logging.getLogger(__name__)

Row = tuple[Any, ...]

#: Redis key prefixes. Namespaced so a FLUSHDB of one concern is possible and so the
#: keyspace is readable during an incident.
LATEST_STATE_KEY: Final[str] = "veh:state:"
WATERMARK_KEY: Final[str] = "veh:mark:"
VEHICLE_CACHE_KEY: Final[str] = "veh:meta:"


# NOTE: PostgreSQL's SET statement accepts no bind parameters. Every runtime
# parameter in this module is therefore applied with set_config(), which does.
# Using SET with string interpolation would be an injection vector (CLAUDE.md S10).


def _set_tenant(cursor: psycopg.Cursor[Row], tenant_id: str) -> None:
    """Apply the RLS tenant context for the rows about to be written.

    ``set_config`` rather than ``SET``: ``SET`` takes no bind parameters, so using it
    would mean interpolating a value into SQL (CLAUDE.md S10).
    """
    cursor.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))


class PostgresSink:
    """Batched, idempotent writes of idle events and alerts.

    Writes are grouped by tenant and each group runs under that tenant's RLS context,
    rather than using the ``BYPASSRLS`` maintenance role. That costs one extra
    ``set_config`` per tenant per batch - negligible - and buys a real guarantee: even
    a bug in the processor cannot write a row into the wrong tenant.
    """

    def __init__(self, settings: PostgresSettings) -> None:
        self._pool = ConnectionPool(
            conninfo=settings.dsn,
            min_size=settings.postgres_pool_min,
            max_size=settings.postgres_pool_max,
            kwargs={"row_factory": psycopg.rows.tuple_row},
            # Fail fast rather than queueing forever behind an exhausted pool.
            timeout=10.0,
            open=True,
        )
        self._statement_timeout_ms = settings.postgres_statement_timeout_ms

    def close(self) -> None:
        self._pool.close()

    def load_vehicles(self, vins: Sequence[str]) -> dict[str, VehicleRef]:
        """Fetch vehicle metadata for a batch of VINs.

        One query with ``= ANY(...)`` rather than a query per VIN: the latter is the
        ORM N+1 pattern the PDF S8 optimisation section explicitly calls out.
        """
        if not vins:
            return {}
        with self._pool.connection() as conn, conn.cursor() as cur:
            # Cross-tenant read: the processor legitimately serves every tenant on its
            # partitions. Uses the BYPASSRLS maintenance role for this lookup only,
            # and only for vehicle metadata - never for writes.
            cur.execute("SET ROLE fleet_batch")
            cur.execute(
                "SELECT vin, tenant_id, powertrain_code, class_code, fleet_id"
                " FROM vehicles WHERE vin = ANY(%s) AND is_active",
                (list(vins),),
            )
            rows = cur.fetchall()
            cur.execute("RESET ROLE")

        from fleet_common.domain.fuel import VehicleClass

        return {
            str(vin): VehicleRef(
                vin=str(vin).strip(),
                tenant_id=str(tenant_id),
                powertrain=Powertrain(powertrain),
                vehicle_class=VehicleClass(class_code),
                fleet_id=int(fleet_id),
            )
            for vin, tenant_id, powertrain, class_code, fleet_id in rows
        }

    def write_idle_events(self, segments: Sequence[PricedIdleSegment]) -> int:
        """Insert closed idle events, skipping any already present.

        ``ON CONFLICT DO NOTHING`` on ``(vin, started_at)`` is what turns
        at-least-once delivery into an effectively-once outcome: a replayed batch
        collides here instead of double-counting fuel (ADR-0004).
        """
        if not segments:
            return 0

        by_tenant: dict[str, list[PricedIdleSegment]] = {}
        for priced in segments:
            by_tenant.setdefault(priced.segment.tenant_id, []).append(priced)

        written = 0
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                # set_config, not SET: SET accepts no bind parameters, so using it
                # would mean interpolating a value into SQL. `true` scopes the
                # setting to this transaction.
                cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(self._statement_timeout_ms),),
                )
                cur.execute("SET ROLE fleet_app")
            for tenant_id, group in by_tenant.items():
                with conn.cursor() as cur:
                    _set_tenant(cur, tenant_id)
                    cur.executemany(
                        """
                        INSERT INTO idle_events (
                            tenant_id, vin, started_at, ended_at, duration_seconds,
                            sample_count, max_gap_seconds, excessive, pto_active_any,
                            start_lat, start_lon,
                            fuel_litres_est, cost_est, currency, formula_version,
                            burn_rate_lph, price_id, idle_threshold_seconds,
                            fuel_litres_measured
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (vin, started_at) DO NOTHING
                        """,
                        [self._idle_row(p) for p in group],
                    )
                    written += cur.rowcount if cur.rowcount > 0 else 0
            conn.commit()
        return written

    @staticmethod
    def _idle_row(priced: PricedIdleSegment) -> Row:
        segment = priced.segment
        estimate = priced.estimate
        return (
            segment.tenant_id,
            segment.vin,
            segment.started_at,
            segment.ended_at,
            segment.duration_seconds,
            segment.sample_count,
            segment.max_gap_seconds,
            segment.excessive,
            segment.pto_active_any,
            segment.start_lat,
            segment.start_lon,
            estimate.litres_estimated if estimate else None,
            estimate.cost if estimate else None,
            estimate.currency if estimate else None,
            estimate.formula_version if estimate else "unpriced",
            estimate.burn_rate_lph if estimate else None,
            estimate.price_id if estimate else None,
            estimate.idle_threshold_seconds if estimate else 0,
            estimate.litres_measured if estimate else None,
        )

    def write_alerts(self, alerts: Sequence[Alert]) -> int:
        """Insert alerts, suppressing repeats for the same idle segment (D10)."""
        if not alerts:
            return 0

        by_tenant: dict[str, list[Alert]] = {}
        for alert in alerts:
            by_tenant.setdefault(alert.tenant_id, []).append(alert)

        written = 0
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET ROLE fleet_app")
            for tenant_id, group in by_tenant.items():
                with conn.cursor() as cur:
                    _set_tenant(cur, tenant_id)
                    cur.executemany(
                        """
                        INSERT INTO alerts (
                            tenant_id, vin, alert_type, severity,
                            triggering_event_ts, idle_event_started_at, detail
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        -- The suppression index is PARTIAL, so the conflict target
                        -- must repeat its predicate for Postgres to infer it.
                        ON CONFLICT (vin, alert_type, idle_event_started_at)
                            WHERE idle_event_started_at IS NOT NULL
                            DO NOTHING
                        """,
                        [
                            (
                                a.tenant_id,
                                a.vin,
                                a.alert_type.value,
                                a.severity,
                                a.triggering_event_ts,
                                a.idle_segment_started_at,
                                orjson.dumps(a.detail).decode(),
                            )
                            for a in group
                        ],
                    )
                    written += cur.rowcount if cur.rowcount > 0 else 0
            conn.commit()
        return written


class MongoSink:
    """Raw telemetry, with a TTL index so the hot tier expires itself.

    At the measured 479 bytes/event, 100K events/s is 4.14 TB/day, so retention here
    is a cost decision, not a preference (CLAUDE.md S8 lifecycle).
    """

    def __init__(self, settings: MongoSettings) -> None:
        self._client: MongoClient[dict[str, Any]] = MongoClient(
            settings.mongo_uri,
            # Bounded waits: an unreachable Mongo must fail the batch, not hang the
            # consumer and stall the whole partition.
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
            socketTimeoutMS=10_000,
        )
        self._collection = self._client[settings.mongo_db][settings.mongo_raw_collection]
        self._ttl_seconds = settings.mongo_raw_ttl_seconds

    def ensure_indexes(self) -> None:
        """Create the TTL and lookup indexes. Idempotent."""
        self._collection.create_index("event_id", unique=True, name="event_id_unique")
        self._collection.create_index([("vin", 1), ("ts", -1)], name="vin_ts")
        # TTL on ingest time, not event time: event time can be backdated by a late
        # event, which would make a document expire the moment it was written.
        self._collection.create_index(
            "ingest_ts", expireAfterSeconds=self._ttl_seconds, name="hot_tier_ttl"
        )

    def close(self) -> None:
        self._client.close()

    def write_events(self, events: Sequence[TelemetryEvent]) -> int:
        """Upsert raw events by ``event_id``.

        Upsert rather than insert: a replayed batch must be inert. ``ordered=False``
        so one bad document does not abandon the rest of the batch.
        """
        if not events:
            return 0

        ingest_ts = datetime.now(UTC)
        operations = [
            UpdateOne(
                {"event_id": event.event_id},
                {
                    "$set": {
                        **orjson.loads(event.model_dump_json()),
                        # Processor-assigned, never read from the device: a device
                        # clock is bug- and attacker-controlled (ADR-0004).
                        "ingest_ts": ingest_ts,
                    }
                },
                upsert=True,
            )
            for event in events
        ]

        try:
            result = self._collection.bulk_write(operations, ordered=False)
        except BulkWriteError as exc:
            # Duplicate-key errors are expected on replay and are not failures.
            genuine = [e for e in exc.details.get("writeErrors", []) if e.get("code") != 11000]
            if genuine:
                raise
            logger.debug("mongo bulk_write absorbed %d duplicate keys", len(exc.details))
            return 0
        return int(result.upserted_count + result.modified_count)


class RedisState:
    """Hot per-vehicle state and dedup watermarks.

    Redis is treated as AP and disposable (CLAUDE.md S8): losing it costs a cache
    rebuild, not data. The watermarks are the one thing worth persisting, because
    without them a restart re-accepts everything in the retention window as new.
    """

    def __init__(self, settings: RedisSettings) -> None:
        self._client: Redis = Redis.from_url(
            settings.redis_url,
            decode_responses=False,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )
        self._ttl = settings.redis_state_ttl_seconds

    def close(self) -> None:
        self._client.close()

    def write_latest_state(self, states: dict[str, LatestState]) -> int:
        """Write latest vehicle state in one pipeline round trip."""
        if not states:
            return 0
        pipe = self._client.pipeline(transaction=False)
        for vin, state in states.items():
            pipe.set(
                f"{LATEST_STATE_KEY}{vin}",
                orjson.dumps(
                    {
                        "vin": state.vin,
                        "tenant_id": state.tenant_id,
                        "ts": state.ts.isoformat(),
                        "speed_kph": state.speed_kph,
                        "ignition": state.ignition,
                        "odometer_km": state.odometer_km,
                        # Coordinates are stored for the live map but must never be
                        # logged (CLAUDE.md S11).
                        "lat": state.lat,
                        "lon": state.lon,
                        "idle_state": state.idle_state,
                        "open_idle_seconds": state.open_idle_seconds,
                    }
                ),
                ex=self._ttl,
            )
        pipe.execute()
        return len(states)

    def save_watermarks(self, watermarks: dict[str, Any]) -> int:
        """Persist dedup watermarks so a restart does not re-accept old events."""
        if not watermarks:
            return 0
        pipe = self._client.pipeline(transaction=False)
        for vin, mark in watermarks.items():
            pipe.set(
                f"{WATERMARK_KEY}{vin}",
                orjson.dumps(
                    {
                        "vin": mark.vin,
                        "highest_seq": mark.highest_seq,
                        "highest_ts": mark.highest_ts.isoformat(),
                        "seen_mask": str(mark.seen_mask),
                        "accepted": mark.accepted,
                        "duplicates": mark.duplicates,
                        "late": mark.late,
                        "beyond_window": mark.beyond_window,
                    }
                ),
                ex=self._ttl,
            )
        pipe.execute()
        return len(watermarks)

    def load_watermarks(self, vins: Iterable[str]) -> dict[str, Any]:
        """Restore watermarks for the given VINs, e.g. after a rebalance."""
        from processor.dedup import VehicleWatermark

        keys = [f"{WATERMARK_KEY}{vin}" for vin in vins]
        if not keys:
            return {}

        restored: dict[str, Any] = {}
        for key, raw in zip(keys, self._client.mget(keys), strict=True):
            if raw is None:
                continue
            data = orjson.loads(raw)
            restored[data["vin"]] = VehicleWatermark(
                vin=data["vin"],
                highest_seq=int(data["highest_seq"]),
                highest_ts=datetime.fromisoformat(data["highest_ts"]),
                seen_mask=int(data["seen_mask"]),
                accepted=int(data["accepted"]),
                duplicates=int(data["duplicates"]),
                late=int(data["late"]),
                beyond_window=int(data.get("beyond_window", 0)),
            )
            del key
        return restored


@dataclass(slots=True)
class WriteCounts:
    """What one batch actually persisted, for metrics and the load-test report."""

    raw_events: int = 0
    idle_events: int = 0
    alerts: int = 0
    states: int = 0
    dlq: int = 0
    late: int = 0


def summarise_rejections(rejections: Sequence[Rejection]) -> dict[str, int]:
    """Count DLQ rejections by reason code.

    Aggregated by code because "how many failed and why" is the first question during
    an incident, and a list of individual messages does not answer it.
    """
    counts: dict[str, int] = {}
    for rejection in rejections:
        counts[rejection.reason.value] = counts.get(rejection.reason.value, 0) + 1
    return counts
