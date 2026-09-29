"""Simulator CLI.

    uv run python -m simulator --vehicles 2000
    uv run python -m simulator --vehicles 100000 --days 1 --dry-run

``--dry-run`` generates and counts without producing to Kafka, which is how the
generator's own throughput is measured separately from the transport's. Keeping
those two numbers apart matters: a single combined figure cannot tell you whether
the simulator or the broker is the bottleneck (CLAUDE.md S9).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from fleet_common.settings import kafka_settings, simulator_settings
from simulator.fleet import build_fleet
from simulator.journey import DaySummary, JourneyConfig, generate_day


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    settings = simulator_settings()
    parser = argparse.ArgumentParser(prog="simulator", description=__doc__)
    parser.add_argument("--vehicles", type=int, default=settings.sim_vehicle_count)
    parser.add_argument("--days", type=int, default=1, help="simulated days per vehicle")
    parser.add_argument(
        "--seed",
        type=int,
        default=settings.sim_seed,
        help="deterministic seed; the same seed always produces identical output",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="ISO start instant for the first simulated day (default: yesterday 05:00Z)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="generate and count without producing to Kafka",
    )
    parser.add_argument("--batch-size", type=int, default=1000, help="events per produce batch")
    parser.add_argument("--progress-every", type=int, default=10_000, help="progress line interval")
    return parser.parse_args(argv)


#: A generated working day spans up to about 18 hours from its start.
SIMULATED_DAY_SPAN = timedelta(hours=18)


def _resolve_start(raw: str | None) -> datetime:
    """Resolve the first simulated day's start instant.

    Defaults to **yesterday** at 05:00 UTC, not today. A generated day runs forward for
    up to 18 hours, so starting it at 05:00 today puts most of the events in the future,
    and the processor rejects anything more than a few minutes ahead as a broken device
    clock. Starting yesterday guarantees the whole day is in the past whatever time the
    simulator is run at.

    This is not hypothetical: running at 06:59 UTC with a today start sent 72% of events
    to the DLQ as `timestamp_too_far_future`.
    """
    if raw is None:
        yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
        return datetime(yesterday.year, yesterday.month, yesterday.day, 5, 0, 0, tzinfo=UTC)

    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise SystemExit("--start must include a timezone offset, e.g. 2026-09-29T05:00:00Z")
    return parsed.astimezone(UTC)


def _warn_if_start_reaches_the_future(start: datetime, days: int) -> None:
    """Warn when the chosen window would generate future-dated events.

    The processor would reject them, and a 70% DLQ rate is far harder to diagnose
    after the fact than a line of output before the run.
    """
    latest = start + timedelta(days=days - 1) + SIMULATED_DAY_SPAN
    now = datetime.now(UTC)
    if latest <= now:
        return
    ahead = latest - now
    print(
        f"WARNING: this window generates events up to {ahead} in the future. "
        "The processor rejects anything more than a few minutes ahead as a broken "
        "device clock, so those events will land in the DLQ. Pass an earlier --start.",
        file=sys.stderr,
    )


def _batched(events: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(events), size):
        yield events[start : start + size]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    start = _resolve_start(args.start)
    _warn_if_start_reaches_the_future(start, args.days)

    print(f"Building roster: {args.vehicles:,} vehicles, seed {args.seed}")
    roster_began = time.perf_counter()
    fleet = build_fleet(vehicle_count=args.vehicles, seed=args.seed)
    roster_seconds = time.perf_counter() - roster_began
    print(f"  roster ready in {roster_seconds:.2f}s")

    rng = np.random.default_rng(args.seed)
    journey_config = JourneyConfig()

    producer = None
    if not args.dry_run:
        # Imported here so --dry-run works on a machine where the native Kafka
        # library cannot load (blocker B2).
        from simulator.adapters.kafka_producer import TelemetryProducer

        producer = TelemetryProducer(kafka_settings())
        print(f"  producing to {kafka_settings().kafka_topic_telemetry}")

    totals = {
        "events": 0,
        "trips": 0,
        "idle": 0,
        "billable_idle": 0,
        "excessive_idle": 0,
        "duplicates": 0,
        "out_of_order": 0,
        "malformed": 0,
        "dropped": 0,
    }
    summaries: list[DaySummary] = []

    began = time.perf_counter()
    for day_index in range(args.days):
        day_start = start + timedelta(days=day_index)
        for position, vehicle in enumerate(fleet, start=1):
            events, summary = generate_day(vehicle, day_start, rng=rng, config=journey_config)
            summaries.append(summary)

            totals["events"] += summary.event_count
            totals["trips"] += summary.trips
            totals["idle"] += summary.idle_periods
            totals["billable_idle"] += summary.billable_idle_periods
            totals["excessive_idle"] += summary.excessive_idle_periods
            totals["duplicates"] += summary.duplicates_injected
            totals["out_of_order"] += summary.out_of_order_injected
            totals["malformed"] += summary.malformed_injected
            totals["dropped"] += summary.samples_dropped

            if producer is not None:
                for batch in _batched(events, args.batch_size):
                    producer.produce_batch(batch)

            if position % args.progress_every == 0:
                elapsed = time.perf_counter() - began
                rate = totals["events"] / elapsed if elapsed else 0.0
                print(
                    f"  {position:,}/{len(fleet):,} vehicles  "
                    f"{totals['events']:,} events  {rate:,.0f} ev/s"
                )

    if producer is not None:
        unacked = producer.flush()
        stats = producer.stats
        print(
            f"\nProducer: enqueued {stats.enqueued:,}  delivered {stats.delivered:,}  "
            f"failed {stats.failed:,}  queue-full waits {stats.queue_full_retries:,}"
        )
        if stats.errors:
            print("  first errors: " + "; ".join(stats.errors[:3]))
        if unacked or not stats.is_lossless:
            # Reported as failure, not a warning. A partial produce is data loss,
            # and a benchmark built on it would be false (CLAUDE.md S3).
            print(
                f"\nFAILED: {unacked:,} message(s) unacknowledged. "
                "This run lost data and must not be cited as evidence.",
                file=sys.stderr,
            )
            return 1

    elapsed = time.perf_counter() - began
    rate = totals["events"] / elapsed if elapsed else 0.0

    mode = "generate-only" if args.dry_run else "generate + produce"
    print(f"\nGenerated {totals['events']:,} events in {elapsed:.2f}s")
    print(f"  generation rate     : {rate:,.0f} events/s  ({mode})")
    print(f"  trips               : {totals['trips']:,}")
    print(f"  idle periods        : {totals['idle']:,}")
    print(f"    billable (>=180s) : {totals['billable_idle']:,}")
    print(f"    excessive (>=600s): {totals['excessive_idle']:,}")
    print("  injected faults (deliberate, PDF S7):")
    print(f"    duplicates        : {totals['duplicates']:,}")
    print(f"    out-of-order      : {totals['out_of_order']:,}")
    print(f"    malformed         : {totals['malformed']:,}")
    print(f"    dropped samples   : {totals['dropped']:,}")
    print(
        "\nNote: the rate above is a LOCAL measurement on this machine. It is not "
        "the 100K events/s NFR, which needs cloud-scale hardware."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
