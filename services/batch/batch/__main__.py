"""Batch job CLI: rollups, utilisation scoring and partition management.

    uv run python -m batch rollup --days 7
    uv run python -m batch utilisation
    uv run python -m batch partitions

Designed to be safe to re-run. Every write is an upsert keyed on the rollup key, so
running the same day twice corrects rather than duplicates - which is what makes
late-event reconciliation cheap (D6).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import psycopg.rows

from batch.erasure import erase_driver, verify_erasure
from batch.rollups import (
    DEFAULT_UTILISATION_WINDOW_DAYS,
    compute_fleet_daily,
    compute_utilisation,
    compute_vehicle_daily,
)
from fleet_common.settings import app_settings, postgres_settings

logger = logging.getLogger("batch")


def _configure_logging() -> None:
    logging.basicConfig(
        level=app_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def _tenants(conn: psycopg.Connection[psycopg.rows.TupleRow]) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SET ROLE fleet_batch")
        cur.execute("SELECT tenant_id FROM tenants ORDER BY tenant_id")
        return [str(row[0]) for row in cur.fetchall()]


def run_rollup(days: int, tenant_id: str | None) -> int:
    """Recompute daily rollups for the last ``days`` calendar days."""
    settings = postgres_settings()
    today = datetime.now(UTC).date()
    began = time.perf_counter()
    vehicle_rows = fleet_rows = 0

    with psycopg.connect(settings.dsn, row_factory=psycopg.rows.tuple_row) as conn:
        for offset in range(days):
            day = today - timedelta(days=offset)
            vehicle_rows += compute_vehicle_daily(conn, day=day, tenant_id=tenant_id)
            fleet_rows += compute_fleet_daily(conn, day=day, tenant_id=tenant_id)
            logger.info("rolled up %s", day)

    elapsed = time.perf_counter() - began
    print(f"vehicle_daily rows : {vehicle_rows:,}")
    print(f"fleet_daily rows   : {fleet_rows:,}")
    print(f"elapsed            : {elapsed:.2f}s")
    return 0


def run_utilisation(window_days: int, tenant_id: str | None) -> int:
    """Score utilisation for every tenant over the trailing window."""
    settings = postgres_settings()
    window_end = datetime.now(UTC).date()
    began = time.perf_counter()
    total_scores = total_skipped = total_no_data = 0
    total_class_too_small = total_active = 0

    with psycopg.connect(settings.dsn, row_factory=psycopg.rows.tuple_row) as conn:
        targets = [tenant_id] if tenant_id else _tenants(conn)
        for target in targets:
            assert target is not None
            summary = compute_utilisation(
                conn, tenant_id=target, window_end=window_end, window_days=window_days
            )
            total_scores += summary.scored
            total_skipped += summary.classes_skipped
            total_no_data += summary.unscored_no_telemetry
            total_class_too_small += summary.unscored_class_too_small
            total_active += summary.vehicles_active
            logger.info(
                "%s: %d/%d scored, %d no-data, %d in a class too small, %d class(es) skipped",
                target,
                summary.scored,
                summary.vehicles_active,
                summary.unscored_no_telemetry,
                summary.unscored_class_too_small,
                summary.classes_skipped,
            )

    elapsed = time.perf_counter() - began
    print(f"active vehicles    : {total_active:,}")
    print(f"utilisation scores : {total_scores:,}")
    if total_class_too_small:
        print(
            f"class too small    : {total_class_too_small:,} vehicle(s) left unscored "
            "(fewer peers than a baseline needs)"
        )
    if total_no_data:
        # Stated, not buried. These vehicles are NOT under-used; no telemetry reached us
        # for them, and reporting them as idle assets would send someone to sell a
        # working vehicle.
        print(
            f"no telemetry       : {total_no_data:,} vehicle(s) left unscored "
            "(no data received, which is not the same as unused)"
        )
    if total_skipped:
        # Not a warning to bury: a skipped class means those vehicles have no score at
        # all, and the UI must be able to explain the gap rather than show a blank.
        print(f"classes skipped    : {total_skipped} (too few peers for a usable baseline)")
    print(f"window             : {window_days} days ending {window_end}")
    print(f"elapsed            : {elapsed:.2f}s")
    return 0


def run_partitions(months_forward: int) -> int:
    """Create partitions ahead of time.

    Ahead of time on purpose: an insert arriving before its partition exists lands in
    the DEFAULT partition, and rows there block later creation of an overlapping one.
    """
    settings = postgres_settings()
    created = []
    with psycopg.connect(settings.dsn, row_factory=psycopg.rows.tuple_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SET ROLE fleet_batch")
            for table in ("idle_events", "trips", "audit_log"):
                cur.execute(
                    "SELECT ensure_partitions_around(%s, %s, %s)",
                    (table, 1, months_forward),
                )
                created.append(table)
        conn.commit()

    print(f"ensured partitions for: {', '.join(created)}")
    print(f"window: 1 month back, {months_forward} months forward")
    return 0


def run_default_partition_check() -> int:
    """Report rows sitting in a DEFAULT partition.

    An unmonitored default partition is how a partitioned schema silently rots: rows
    accumulate there, and creating the proper partition for that range then fails.
    """
    settings = postgres_settings()
    problems = []
    with (
        psycopg.connect(settings.dsn, row_factory=psycopg.rows.tuple_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SET ROLE fleet_batch")
        for table in ("idle_events_default", "trips_default", "audit_log_default"):
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed list
            row = cur.fetchone()
            count = int(row[0]) if row else 0
            if count:
                problems.append((table, count))

    if not problems:
        print("no rows in any DEFAULT partition")
        return 0

    for table, count in problems:
        print(f"WARNING: {count:,} row(s) in {table}", file=sys.stderr)
    print(
        "\nRows here fall outside every prepared partition. Create the partition for "
        "their range and move them, or the range can never be partitioned.",
        file=sys.stderr,
    )
    return 1


def run_erase_driver(tenant_id: str, driver_id: int, requested_by: str) -> int:
    """Erase one driver's personal data across every store (PDF S11).

    Reports per store rather than as one total, so a partial erasure is visible: "it
    worked" across three systems is a claim that needs evidence for each.
    """
    settings = postgres_settings()

    # Typed as Any: erase_driver treats these as optional handles and records a failure
    # rather than raising, so a store being unreachable is reported, not fatal.
    mongo_client: Any = None
    redis_client: Any = None
    try:
        from pymongo import MongoClient

        from fleet_common.settings import mongo_settings, redis_settings

        mongo_client = MongoClient(mongo_settings().mongo_uri, serverSelectionTimeoutMS=5_000)
        from redis import Redis

        redis_client = Redis.from_url(redis_settings().redis_url, socket_timeout=5.0)
    except Exception as exc:
        logger.warning("a store is unreachable; erasure will record it: %s", exc)

    result = erase_driver(
        dsn=settings.dsn,
        tenant_id=tenant_id,
        driver_id=driver_id,
        requested_by=requested_by,
        mongo_client=mongo_client,
        redis_client=redis_client,
    )

    print(f"erasure record     : {result.erasure_id}")
    print(f"drivers anonymised : {result.drivers_anonymised}")
    print(f"assignments closed : {result.assignments_severed}")
    print(f"audit rows blanked : {result.audit_rows_anonymised}")
    print(f"mongo documents    : {result.mongo_documents_updated}")
    print(f"redis keys removed : {result.redis_keys_removed}")
    print()
    # Stated explicitly. A deletion that does not say what it left behind invites the
    # question later, and these rows are the fleet's own business record.
    print("retained by design (the fleet's operational record, link severed):")
    print(f"  trips            : {result.trips_retained}")
    print(f"  idle events      : {result.idle_events_retained}")

    if not result.complete:
        print(file=sys.stderr)
        print("ERRORS:", file=sys.stderr)
        for error in result.errors:
            print(f"  {error}", file=sys.stderr)
        print(file=sys.stderr)
        print(
            "The erasure is INCOMPLETE and the audit record says so. "
            "Re-run once the failing store is reachable.",
            file=sys.stderr,
        )
        return 1

    with psycopg.connect(settings.dsn, row_factory=psycopg.rows.tuple_row) as conn:
        verdict = verify_erasure(conn, tenant_id=tenant_id, driver_id=driver_id)
    # Verified by reading the data back, not by trusting the eraser's own report.
    print()
    print(f"verified erased    : {verdict['erased']}")
    return 0 if verdict["erased"] else 1


def run_reconcile_late(max_messages: int, dry_run: bool) -> int:
    """Drain telemetry.late and put the derived record right (ADR-0007).

    Reuses the processor's own segmentation and the batch rollups rather than
    reimplementing either: a second definition of an idle event would eventually
    disagree with the first.
    """
    from batch.adapters.late_topic import LateTopicDrain, decode_late_events
    from batch.adapters.mongo_samples import MongoSampleSource
    from batch.reconcile import (
        ReconciliationCounts,
        fold_outcomes,
        group_by_vehicle_day,
        reconcile_vehicle_day,
        replace_vehicle_day,
        rollup_days_for,
    )
    from fleet_common.settings import cost_settings, kafka_settings, mongo_settings

    settings = postgres_settings()
    counts = ReconciliationCounts()
    began = time.perf_counter()

    drain = LateTopicDrain(kafka_settings())
    source = MongoSampleSource(mongo_settings())
    try:
        raw = drain.drain(max_messages=max_messages)
        events, decode_failures = decode_late_events(raw)
        counts.events_consumed = len(raw)
        counts.errors.extend(decode_failures)

        if not events:
            print("nothing on the late topic")
            _record_reconciliation(settings.dsn, counts, began)
            return 0

        grouped = group_by_vehicle_day(events)
        logger.info("%d late event(s) across %d vehicle-day(s)", len(events), len(grouped))

        run_id = _record_reconciliation(settings.dsn, counts, began, open_only=True)

        with psycopg.connect(settings.dsn, row_factory=psycopg.rows.tuple_row) as conn:
            from processor.__main__ import build_pipeline_config, load_prices
            from processor.adapters.sinks import PostgresSink
            from processor.pipeline import BatchProcessor

            prices = load_prices(settings.dsn, cost_settings().default_currency)
            config = build_pipeline_config(prices)
            sink = PostgresSink(settings)
            try:
                vins = sorted({key.vin for key in grouped})
                vehicles = sink.load_vehicles(vins)

                outcomes = []
                reconciled_keys = []
                for key in sorted(grouped):
                    outcome, segments, trips = reconcile_vehicle_day(
                        key,
                        grouped[key],
                        source=source,
                        segmenter=lambda: BatchProcessor(config),
                        vehicles=vehicles,
                        now=datetime.now(UTC),
                    )
                    outcomes.append(outcome)
                    if not outcome.reconciled or dry_run:
                        continue
                    try:
                        replace_vehicle_day(
                            conn,
                            key,
                            idle_writer=sink.write_idle_events,
                            trip_writer=sink.write_trips,
                            segments=segments,
                            trips=trips,
                        )
                        reconciled_keys.append(key)
                    except psycopg.Error as exc:
                        conn.rollback()
                        counts.errors.append(f"{key}: {exc}")

                fold_outcomes(outcomes, counts)

                if reconciled_keys and not dry_run:
                    for tenant_id, day in sorted(rollup_days_for(reconciled_keys)):
                        compute_vehicle_daily(conn, day=day, tenant_id=tenant_id)
                        compute_fleet_daily(conn, day=day, tenant_id=tenant_id)
                        counts.rollup_days_recomputed += 1
            finally:
                sink.close()

        # Offsets advance only once the work is durable, matching the processor. A late
        # event nothing will resend must not be lost to an optimistic commit.
        if not dry_run and not counts.errors:
            drain.commit()
        _record_reconciliation(settings.dsn, counts, began, run_id=run_id)
    finally:
        source.close()
        drain.close()

    elapsed = time.perf_counter() - began
    print(f"events consumed    : {counts.events_consumed:,}")
    print(f"vehicle-days fixed : {counts.vehicle_days_reconciled:,}")
    print(f"idle events written: {counts.idle_events_written:,}")
    print(f"trips written      : {counts.trips_written:,}")
    print(f"rollup days redone : {counts.rollup_days_recomputed:,}")
    if counts.events_unreconcilable:
        # Never buried. This is the number that says whether the hot-tier TTL is costing
        # the fleet corrected figures, and it is the reason this log exists at all.
        print(f"UNRECONCILABLE     : {counts.events_unreconcilable:,} event(s)")
        for reason, n in sorted(counts.unreconcilable_reasons.items()):
            print(f"  {reason}: {n} vehicle-day(s)")
        print("  These events were NOT discarded; they are recorded in reconciliation_log.")
    if dry_run:
        print("DRY RUN - nothing was written and no offsets were committed")
    print(f"elapsed            : {elapsed:.2f}s")

    if counts.errors:
        print(file=sys.stderr)
        for error in counts.errors:
            print(f"  {error}", file=sys.stderr)
        print("offsets were NOT committed; re-run once fixed", file=sys.stderr)
        return 1
    return 0


def _record_reconciliation(
    dsn: str,
    counts: Any,
    began: float,
    *,
    open_only: bool = False,
    run_id: int | None = None,
) -> int:
    """Open or complete the run's audit row.

    Opened before any write and completed after, like the erasure log: a run that fails
    half way must still leave a record that it was attempted.
    """
    import orjson

    with psycopg.connect(dsn, row_factory=psycopg.rows.tuple_row) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE fleet_batch")
        if run_id is None:
            cur.execute(
                "INSERT INTO reconciliation_log (events_consumed) VALUES (%s) RETURNING run_id",
                (counts.events_consumed,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("failed to open a reconciliation audit record")
            new_id = int(row[0])
            if open_only:
                conn.commit()
                return new_id
            run_id = new_id

        cur.execute(
            "UPDATE reconciliation_log SET finished_at = now(), events_consumed = %s,"
            " vehicle_days_reconciled = %s, idle_events_written = %s, trips_written = %s,"
            " rollup_days_recomputed = %s, events_unreconcilable = %s,"
            " unreconcilable_reasons = %s, errors = %s WHERE run_id = %s",
            (
                counts.events_consumed,
                counts.vehicle_days_reconciled,
                counts.idle_events_written,
                counts.trips_written,
                counts.rollup_days_recomputed,
                counts.events_unreconcilable,
                orjson.dumps(dict(counts.unreconcilable_reasons)).decode(),
                orjson.dumps(counts.errors).decode(),
                run_id,
            ),
        )
        conn.commit()
    return run_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="batch", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    rollup = sub.add_parser("rollup", help="recompute daily rollups")
    rollup.add_argument("--days", type=int, default=7)
    rollup.add_argument("--tenant", type=str, default=None)

    utilisation = sub.add_parser("utilisation", help="score vehicle utilisation")
    utilisation.add_argument("--window-days", type=int, default=DEFAULT_UTILISATION_WINDOW_DAYS)
    utilisation.add_argument("--tenant", type=str, default=None)

    partitions = sub.add_parser("partitions", help="create partitions ahead of time")
    partitions.add_argument("--months-forward", type=int, default=2)

    sub.add_parser("check-default-partitions", help="report rows in DEFAULT partitions")

    reconcile = sub.add_parser(
        "reconcile-late", help="drain telemetry.late and recompute affected vehicle-days"
    )
    reconcile.add_argument("--max-messages", type=int, default=100_000)
    reconcile.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing or committing offsets",
    )

    erase = sub.add_parser("erase-driver", help="erase a driver's personal data")
    erase.add_argument("--tenant", type=str, required=True)
    erase.add_argument("--driver-id", type=int, required=True)
    erase.add_argument(
        "--requested-by",
        type=str,
        required=True,
        help="who requested the erasure; recorded in the audit trail",
    )

    args = parser.parse_args(argv)
    _configure_logging()

    try:
        if args.command == "rollup":
            return run_rollup(args.days, args.tenant)
        if args.command == "utilisation":
            return run_utilisation(args.window_days, args.tenant)
        if args.command == "partitions":
            return run_partitions(args.months_forward)
        if args.command == "check-default-partitions":
            return run_default_partition_check()
        if args.command == "reconcile-late":
            return run_reconcile_late(args.max_messages, args.dry_run)
        if args.command == "erase-driver":
            return run_erase_driver(args.tenant, args.driver_id, args.requested_by)
    except psycopg.Error as exc:
        # Never echo the DSN: it carries the password.
        print(f"database error: {exc}", file=sys.stderr)
        return 2

    # argparse has required=True on the subparser, so an unknown command never reaches
    # here. Kept as a guard rather than dead code: it fails loudly if a subcommand is
    # ever added without a branch above.
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
