"""Processor service entry point.

The loop is deliberately boring, because its ordering is the correctness argument:

    poll -> process (pure) -> persist -> flush side topics -> COMMIT

Offsets are committed last. A crash anywhere before the commit replays the batch, and
the idempotent sinks absorb the replay. Committing earlier would lose data silently,
which is the failure mode that is hardest to notice and worst to explain.

    uv run python -m processor
    uv run python -m processor --once   # one batch, for smoke tests
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import FrameType
from typing import Any

import psycopg
import psycopg.rows

from fleet_common.domain.fuel import FuelPrice
from fleet_common.domain.idle import IdleConfig
from fleet_common.domain.trips import TripConfig
from fleet_common.settings import (
    app_settings,
    cost_settings,
    idle_settings,
    kafka_settings,
    mongo_settings,
    postgres_settings,
    redis_settings,
    stream_settings,
)
from processor.adapters.kafka_io import (
    SideTopicProducer,
    TelemetryConsumer,
    encode_idle_event,
)
from processor.adapters.sinks import (
    MongoSink,
    PostgresSink,
    RedisState,
    WriteCounts,
    summarise_rejections,
)
from processor.dedup import DedupConfig
from processor.pipeline import BatchProcessor, PipelineConfig, VehicleRef
from processor.validation import ValidationConfig

logger = logging.getLogger("processor")


@dataclass(slots=True)
class RunTotals:
    """Cumulative counters for the run, reported on shutdown."""

    batches: int = 0
    consumed: int = 0
    accepted: int = 0
    duplicates: int = 0
    late: int = 0
    rejected: int = 0
    idle_events: int = 0
    trips: int = 0
    alerts: int = 0
    unknown_vehicle: int = 0
    consume_errors: int = 0
    dlq_reasons: dict[str, int] = field(default_factory=dict)

    def merge_reasons(self, reasons: dict[str, int]) -> None:
        for reason, count in reasons.items():
            self.dlq_reasons[reason] = self.dlq_reasons.get(reason, 0) + count


class _Shutdown:
    """Cooperative shutdown flag.

    A signal must not abort mid-batch: that would leave the batch persisted but
    uncommitted, or worse, half persisted. The loop finishes its current batch, then
    exits.
    """

    def __init__(self) -> None:
        self.requested = False

    def request(self, signum: int, _frame: FrameType | None) -> None:
        logger.info("signal %s received; finishing the current batch then exiting", signum)
        self.requested = True


def load_prices(dsn: str, currency: str) -> dict[str, FuelPrice]:
    """Load the current fuel price per fuel type from the temporal price table.

    Reads the row with ``effective_to IS NULL``, which a unique index guarantees is
    single. A cost figure references the ``price_id`` it used, so it stays reproducible
    after prices change (D11).
    """
    prices: dict[str, FuelPrice] = {}
    with psycopg.connect(dsn, row_factory=psycopg.rows.tuple_row) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE fleet_batch")
        cur.execute(
            "SELECT DISTINCT ON (fuel_type) fuel_type, price_id, price_per_litre,"
            " currency FROM fuel_prices WHERE effective_to IS NULL"
            " ORDER BY fuel_type, effective_from DESC"
        )
        for fuel_type, price_id, price_per_litre, price_currency in cur.fetchall():
            prices[str(fuel_type)] = FuelPrice(
                price_id=str(price_id),
                price_per_litre=Decimal(price_per_litre),
                currency=str(price_currency),
            )

    if not prices:
        # Loud, because every cost figure depends on it. The pipeline still runs and
        # records segments without a cost rather than inventing one.
        logger.error(
            "no current fuel prices found; idle events will be recorded WITHOUT a "
            "cost estimate. Run `python -m batch.seed` to load prices."
        )
    else:
        logger.info(
            "loaded %d current price(s): %s",
            len(prices),
            ", ".join(f"{k}={v.price_per_litre} {v.currency}" for k, v in prices.items()),
        )
        mismatched = [k for k, v in prices.items() if v.currency != currency]
        if mismatched:
            logger.warning(
                "price currency differs from DEFAULT_CURRENCY=%s for: %s",
                currency,
                mismatched,
            )
    return prices


def build_pipeline_config(prices: dict[str, FuelPrice]) -> PipelineConfig:
    """Assemble business parameters from configuration only (CLAUDE.md S7)."""
    idle = idle_settings()
    stream = stream_settings()
    return PipelineConfig(
        trip=TripConfig(),
        idle=IdleConfig(
            min_seconds=idle.idle_min_seconds,
            entry_speed_kph=idle.idle_entry_speed_kph,
            exit_speed_kph=idle.idle_exit_speed_kph,
            excessive_seconds=idle.idle_excessive_seconds,
            gap_tolerance_seconds=idle.idle_gap_tolerance_seconds,
            exclude_pto_active=idle.idle_exclude_pto_active,
        ),
        dedup=DedupConfig(allowed_lateness_seconds=stream.allowed_lateness_seconds),
        validation=ValidationConfig(),
        prices=prices,
    )


def _configure_logging() -> None:
    settings = app_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="processor", description=__doc__)
    parser.add_argument("--once", action="store_true", help="process a single batch then exit")
    parser.add_argument(
        "--max-batches", type=int, default=0, help="stop after N batches (0 = unlimited)"
    )
    parser.add_argument(
        "--idle-exit-seconds",
        type=float,
        default=0.0,
        help="exit after this many seconds with no messages (0 = never)",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    kafka = kafka_settings()
    pg = postgres_settings()

    prices = load_prices(pg.dsn, cost_settings().default_currency)
    config = build_pipeline_config(prices)
    processor = BatchProcessor(config)

    postgres = PostgresSink(pg)
    mongo = MongoSink(mongo_settings())
    mongo.ensure_indexes()
    redis_state = RedisState(redis_settings())
    side = SideTopicProducer(kafka)

    # Vehicle metadata cache. Bounded by the vehicles on assigned partitions, and a
    # miss is cheap, so a plain dict is the right structure here.
    vehicle_cache: dict[str, VehicleRef] = {}

    def on_revoke(partitions: list[str]) -> None:
        """Persist everything in flight before the partitions move away."""
        logger.info("flushing before revocation of %s", partitions)
        closed = processor.flush_open_segments(vehicle_cache)
        if closed:
            postgres.write_idle_events(closed)
        open_trips = processor.flush_open_trips(vehicle_cache)
        if open_trips:
            postgres.write_trips(open_trips)
        redis_state.save_watermarks(processor.dedup_state.watermarks)

    consumer = TelemetryConsumer(kafka, on_revoke=on_revoke)
    consumer.subscribe()

    shutdown = _Shutdown()
    signal.signal(signal.SIGINT, shutdown.request)
    signal.signal(signal.SIGTERM, shutdown.request)

    totals = RunTotals()
    began = time.perf_counter()
    last_message_at = time.perf_counter()

    logger.info(
        "processor started: topic=%s group=%s batch=%d",
        kafka.kafka_topic_telemetry,
        kafka.kafka_consumer_group,
        kafka.kafka_batch_max_messages,
    )

    try:
        while not shutdown.requested:
            payloads, consume_errors = consumer.poll_batch(
                kafka.kafka_batch_max_messages, kafka.kafka_poll_timeout_seconds
            )
            totals.consume_errors += consume_errors

            if not payloads:
                if (
                    args.idle_exit_seconds
                    and time.perf_counter() - last_message_at > args.idle_exit_seconds
                ):
                    logger.info("no messages for %.0fs; exiting", args.idle_exit_seconds)
                    break
                continue

            last_message_at = time.perf_counter()
            counts = _process_and_persist(
                payloads=payloads,
                processor=processor,
                postgres=postgres,
                mongo=mongo,
                redis_state=redis_state,
                side=side,
                vehicle_cache=vehicle_cache,
                totals=totals,
            )

            # Side topics must be durable before the commit: otherwise a crash could
            # lose the record that an event was rejected while the offset says done.
            if side.flush() == 0:
                consumer.commit()
            else:
                logger.error("not committing: side topics still have unsent messages")

            totals.batches += 1
            if args.once or (args.max_batches and totals.batches >= args.max_batches):
                break

            del counts
    finally:
        logger.info("shutting down; flushing open idle segments")
        closed = processor.flush_open_segments(vehicle_cache)
        if closed:
            written = postgres.write_idle_events(closed)
            totals.idle_events += written
            logger.info("flushed %d open idle segment(s)", written)
        open_trips = processor.flush_open_trips(vehicle_cache)
        if open_trips:
            written_trips = postgres.write_trips(open_trips)
            totals.trips += written_trips
            logger.info("flushed %d open trip(s)", written_trips)
        redis_state.save_watermarks(processor.dedup_state.watermarks)
        side.flush()
        consumer.close()
        postgres.close()
        mongo.close()
        redis_state.close()

    _report(totals, time.perf_counter() - began, processor)
    return 0


def _process_and_persist(
    *,
    payloads: list[bytes],
    processor: BatchProcessor,
    postgres: PostgresSink,
    mongo: MongoSink,
    redis_state: RedisState,
    side: SideTopicProducer,
    vehicle_cache: dict[str, VehicleRef],
    totals: RunTotals,
) -> WriteCounts:
    """Process one batch and persist everything it produced."""
    counts = WriteCounts()

    # Vehicle metadata is needed before processing, because cost depends on powertrain
    # and class, which live on the vehicle master rather than on the event (ADR-0004).
    vins = _peek_vins(payloads)
    missing = [vin for vin in vins if vin not in vehicle_cache]
    if missing:
        vehicle_cache.update(postgres.load_vehicles(missing))

    result = processor.process(payloads, vehicle_cache, now=datetime.now(UTC))

    totals.consumed += result.total_consumed
    totals.accepted += len(result.accepted)
    totals.duplicates += result.duplicates
    totals.late += len(result.late)
    totals.rejected += len(result.rejections)
    totals.unknown_vehicle += result.unknown_vehicle
    totals.merge_reasons(summarise_rejections(result.rejections))

    # Raw telemetry first: it is the source everything else can be rebuilt from, so if
    # any single write is going to succeed it should be this one.
    counts.raw_events = mongo.write_events(result.accepted)

    if result.trips:
        # Trips before idle events: utilisation is derived from trips, so on a partial
        # write the more fundamental record should be the one that survives.
        totals.trips += postgres.write_trips(result.trips)

    if result.idle_segments:
        counts.idle_events = postgres.write_idle_events(result.idle_segments)
        side.publish_idle_events([encode_idle_event(p) for p in result.idle_segments])
        totals.idle_events += counts.idle_events

    if result.alerts:
        counts.alerts = postgres.write_alerts(result.alerts)
        totals.alerts += counts.alerts

    counts.states = redis_state.write_latest_state(result.latest_state)
    redis_state.save_watermarks(processor.dedup_state.watermarks)

    if result.rejections:
        side.publish_rejections(result.rejections)
        counts.dlq = len(result.rejections)
    if result.late:
        side.publish_late(result.late)
        counts.late = len(result.late)

    return counts


def _peek_vins(payloads: list[bytes]) -> list[str]:
    """Extract candidate VINs without full validation.

    A cheap pre-pass so vehicle metadata is fetched once per batch rather than once per
    event. Malformed payloads are ignored here; the pipeline rejects them properly.
    """
    import orjson

    seen: set[str] = set()
    for payload in payloads:
        try:
            parsed: Any = orjson.loads(payload)
        except orjson.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            vin = parsed.get("vin")
            if isinstance(vin, str):
                seen.add(vin)
    return sorted(seen)


def _report(totals: RunTotals, elapsed: float, processor: BatchProcessor) -> None:
    rate = totals.consumed / elapsed if elapsed else 0.0
    dedup = processor.dedup_state.totals

    logger.info("--- run summary ---")
    logger.info("batches              : %d", totals.batches)
    logger.info("messages consumed    : %d", totals.consumed)
    logger.info("  accepted           : %d", totals.accepted)
    logger.info("  duplicates dropped : %d", totals.duplicates)
    logger.info("  late -> late topic : %d", totals.late)
    logger.info("  rejected -> DLQ    : %d", totals.rejected)
    for reason, count in sorted(totals.dlq_reasons.items(), key=lambda kv: -kv[1]):
        logger.info("      %-28s %d", reason, count)
    logger.info("idle events written  : %d", totals.idle_events)
    logger.info("trips written        : %d", totals.trips)
    logger.info("alerts written       : %d", totals.alerts)
    logger.info("unknown vehicles     : %d", totals.unknown_vehicle)
    logger.info("vehicles tracked     : %d", processor.tracked_vehicles)
    logger.info("beyond dedup window  : %d", dedup.get("beyond_window", 0))
    logger.info("consume errors       : %d", totals.consume_errors)
    logger.info("elapsed              : %.2fs", elapsed)
    logger.info("throughput           : %.0f events/s (LOCAL, not the NFR)", rate)

    # Accounting check: nothing may vanish silently.
    handled = totals.accepted + totals.duplicates + totals.late + totals.rejected
    if handled != totals.consumed:
        logger.error(
            "ACCOUNTING MISMATCH: consumed %d but handled %d - events are being lost",
            totals.consumed,
            handled,
        )


if __name__ == "__main__":
    raise SystemExit(run())
