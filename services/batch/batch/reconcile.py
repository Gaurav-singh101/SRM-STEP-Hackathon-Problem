"""Late-event reconciliation (ADR-0007, CLAUDE.md §8).

The processor publishes events that arrive past their vehicle's lateness window to
``telemetry.late`` rather than discarding them. This is what consumes that topic and puts
the derived record right, which is what makes "never silently drop" true rather than
merely intended.

**A late event is a raw sample; the thing that has to change is a derived segment.** An
idle event is produced by a state machine folding over an ordered run of samples for one
vehicle. A sample arriving after that segment was written may extend it, split it, merge
two, or create a new one, so it cannot be patched in. Reconciliation therefore means
recomputing the affected **vehicle-day** from its complete sample set.

That recomputation deliberately runs the **same** ``BatchProcessor`` the stream uses, on a
fresh instance with no watermark state. The alternative - a bespoke re-segmentation path -
would be a second definition of an idle event, and two definitions eventually disagree.
Because the machine is fresh, the result is what the stream would have produced had every
event arrived in order, which is the correctness claim worth testing.

**The limit is the hot tier, and it is counted rather than hidden.** Raw samples live under
a TTL sized by cost. An event later than that cannot be reconciled, and rebuilding a
segment from half its samples would be worse than leaving it stale because the result would
look current. Those events are recorded with a reason and a count so an operator can decide
whether to raise the TTL.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from fleet_common.events import TelemetryEvent

logger = logging.getLogger(__name__)


class UnreconcilableReason:
    """Reason codes, enumerated so the log can be aggregated.

    A count per reason answers "what is stopping reconciliation" in one query. Free text
    would not, for the same reason the DLQ uses codes rather than prose.
    """

    RAW_SAMPLES_EXPIRED = "raw_samples_expired"
    NO_SAMPLES_FOUND = "no_samples_found"
    VEHICLE_UNKNOWN = "vehicle_unknown"
    RECOMPUTE_FAILED = "recompute_failed"


@dataclass(frozen=True, slots=True, order=True)
class VehicleDay:
    """The unit of reconciliation work.

    A day rather than a segment, because a segment's boundaries are an *output* of
    reconciliation and cannot be known before it runs.
    """

    tenant_id: str
    vin: str
    day: date

    def __str__(self) -> str:
        return f"{self.tenant_id}/{self.vin}@{self.day}"


class SampleSource(Protocol):
    """Where a vehicle-day's complete raw samples come from.

    A port rather than a direct MongoDB call, because the cold tier is still open (the rest
    of D6). If Parquet on object storage lands later, reconciliation can read from it beyond
    the Mongo TTL and the gap below narrows without a 15x hot-tier bill - by substituting an
    implementation, not by rewriting this module (ADR-0007).
    """

    def retention_floor(self) -> datetime:
        """The earliest event time still guaranteed to be retained in full."""
        ...

    def samples_for(self, *, vin: str, day: date) -> list[bytes]:
        """Every retained raw sample for that vehicle-day, as encoded events."""
        ...


@dataclass(slots=True)
class ReconciliationCounts:
    """What one run did, mirroring the ``reconciliation_log`` columns."""

    events_consumed: int = 0
    vehicle_days_reconciled: int = 0
    idle_events_written: int = 0
    trips_written: int = 0
    rollup_days_recomputed: int = 0
    events_unreconcilable: int = 0
    unreconcilable_reasons: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether every consumed event was reconciled.

        Deliberately not the same as "the run succeeded". A run that correctly reports 412
        expired events succeeded; it just did not reconcile everything, and conflating those
        two is how a gap becomes invisible.
        """
        return not self.errors and self.events_unreconcilable == 0

    def summary(self) -> dict[str, Any]:
        return {
            "events_consumed": self.events_consumed,
            "vehicle_days_reconciled": self.vehicle_days_reconciled,
            "idle_events_written": self.idle_events_written,
            "trips_written": self.trips_written,
            "rollup_days_recomputed": self.rollup_days_recomputed,
            "events_unreconcilable": self.events_unreconcilable,
            "unreconcilable_reasons": dict(self.unreconcilable_reasons),
            "errors": self.errors,
        }


@dataclass(frozen=True, slots=True)
class DayOutcome:
    """The result of attempting one vehicle-day."""

    key: VehicleDay
    #: ``None`` when the day was reconciled; a reason code when it could not be.
    reason: str | None
    idle_events: int = 0
    trips: int = 0
    #: Events that belonged to this day, so an unreconcilable day counts them all.
    events: int = 0

    @property
    def reconciled(self) -> bool:
        return self.reason is None


def group_by_vehicle_day(
    events: Sequence[TelemetryEvent],
) -> dict[VehicleDay, list[TelemetryEvent]]:
    """Group late events into units of work.

    Keyed on the **UTC** date of the event. ``vehicle_daily`` is keyed on the tenant's local
    day, so the caller recomputes the neighbouring days too - see
    ``rollup_days_for``. Grouping on UTC here keeps this function free of timezone lookups
    and therefore pure; getting the local day subtly wrong would silently recompute the
    wrong rollup, which is worse than recomputing one extra.

    Raises:
        ValueError: on a naive timestamp. This is not defensive boilerplate - a test caught
            it doing the wrong thing. ``datetime.astimezone(UTC)`` on a naive value assumes
            the *system's* local time and converts without complaint, so a naive 01:00 would
            land on the previous UTC day on a machine east of Greenwich and on the same day
            elsewhere. That is the silent mis-grouping this docstring claims to avoid, and
            the machine's timezone is not a fact about the data. The event contract already
            rejects naive timestamps at ingest, but reconciliation reads from a different
            path, so it checks again rather than inheriting an assumption.
    """
    grouped: dict[VehicleDay, list[TelemetryEvent]] = {}
    for event in events:
        if event.ts.tzinfo is None or event.ts.tzinfo.utcoffset(event.ts) is None:
            raise ValueError(
                f"event for {event.vin} has a naive timestamp {event.ts!r}; "
                "reconciliation cannot infer a date from it"
            )
        key = VehicleDay(
            tenant_id=event.tenant_id, vin=event.vin, day=event.ts.astimezone(UTC).date()
        )
        grouped.setdefault(key, []).append(event)
    return grouped


def rollup_days_for(keys: Sequence[VehicleDay]) -> set[tuple[str, date]]:
    """The (tenant, day) rollups a set of reconciled vehicle-days invalidates.

    Includes the day either side of each. A segment starting at 23:50 UTC falls on a
    different local day for most tenants, and the rollup is keyed on the local day, so
    recomputing only the UTC date would leave a stale figure next to a corrected one.
    Recomputing three days is cheap and every rollup write is an upsert, so the cost of
    being generous here is far below the cost of being wrong.
    """
    days: set[tuple[str, date]] = set()
    for key in keys:
        for offset in (-1, 0, 1):
            days.add((key.tenant_id, key.day + timedelta(days=offset)))
    return days


def is_reconcilable(key: VehicleDay, *, retention_floor: datetime) -> bool:
    """Whether that vehicle-day's samples can still be complete.

    The test is the **start** of the day, not the late event's own timestamp. What matters
    is whether the whole day is still retained: if the earliest part of it has expired, the
    sample set is a fragment, and a segment rebuilt from a fragment looks current while
    being wrong.
    """
    day_start = datetime(key.day.year, key.day.month, key.day.day, tzinfo=UTC)
    return day_start >= retention_floor


def reconcile_vehicle_day(
    key: VehicleDay,
    late_events: Sequence[TelemetryEvent],
    *,
    source: SampleSource,
    segmenter: Any,
    vehicles: dict[str, Any],
    now: datetime,
) -> tuple[DayOutcome, list[Any], list[Any]]:
    """Recompute one vehicle-day from its complete sample set.

    ``segmenter`` is a factory returning a **fresh** ``BatchProcessor``. Fresh matters: a
    processor carrying watermarks would classify these very events as late again and refuse
    them, which is how the events reached this module in the first place.

    Returns the outcome plus the idle segments and trips to persist. Persistence is the
    caller's job so this stays free of I/O and testable without a database.
    """
    event_count = len(late_events)

    if key.vin not in vehicles:
        # Without a powertrain there is no burn rate, and without a burn rate there is no
        # cost. Reporting it beats writing a segment with a silently absent figure.
        return DayOutcome(key, UnreconcilableReason.VEHICLE_UNKNOWN, events=event_count), [], []

    if not is_reconcilable(key, retention_floor=source.retention_floor()):
        return DayOutcome(key, UnreconcilableReason.RAW_SAMPLES_EXPIRED, events=event_count), [], []

    raw = source.samples_for(vin=key.vin, day=key.day)
    if not raw:
        # Inside the retention window but nothing there. Distinct from expiry: this is a
        # gap that should not exist, and conflating the two would hide it.
        return DayOutcome(key, UnreconcilableReason.NO_SAMPLES_FOUND, events=event_count), [], []

    processor = segmenter()
    try:
        # The same path the stream takes. Ordering is the source's responsibility; the
        # state machine assumes event-time order and a shuffled run would cut segments
        # arbitrarily.
        processor.process(raw, vehicles, now=now)
        # Flush, because the day is closed by definition: anything still open at the end of
        # a completed day is a segment, not a continuation.
        segments = processor.flush_open_segments(vehicles)
        trips = processor.flush_open_trips(vehicles)
    except Exception as exc:
        logger.warning("recompute failed for %s: %s", key, exc)
        return DayOutcome(key, UnreconcilableReason.RECOMPUTE_FAILED, events=event_count), [], []

    return (
        DayOutcome(key, None, idle_events=len(segments), trips=len(trips), events=event_count),
        segments,
        trips,
    )


def fold_outcomes(outcomes: Sequence[DayOutcome], counts: ReconciliationCounts) -> None:
    """Accumulate per-day outcomes into the run's counters.

    Separated from the loop that produces them so the accounting can be tested against
    hand-written outcomes, which is where an off-by-one in a report actually hides.
    """
    for outcome in outcomes:
        if outcome.reconciled:
            counts.vehicle_days_reconciled += 1
            counts.idle_events_written += outcome.idle_events
            counts.trips_written += outcome.trips
        else:
            counts.events_unreconcilable += outcome.events
            assert outcome.reason is not None
            counts.unreconcilable_reasons[outcome.reason] += 1


def replace_vehicle_day(
    conn: Any,
    key: VehicleDay,
    *,
    idle_writer: Any,
    trip_writer: Any,
    segments: Sequence[Any],
    trips: Sequence[Any],
) -> tuple[int, int]:
    """Replace one vehicle-day's segments, in a single transaction.

    Delete-then-insert rather than upsert, and the reason is the amendment in ADR-0007.
    ``ON CONFLICT (vin, started_at) DO NOTHING`` is correct for the stream - a replayed batch
    must be inert - and wrong here in two ways:

    * A late sample that **extends** a segment keeps the same ``started_at``, so DO NOTHING
      preserves the stale duration and the stale cost while the run reports success.
    * A late sample that **fills a gap** merges two segments into one. Inserting the merged
      row leaves both originals in place, and the same idle minutes are then billed twice.

    The second is the characteristic effect of a late event on gap-tolerant segmentation, and
    it inflates cost rather than under-reporting it, so it cannot be left to a later fix.

    The delete is bounded by ``vin`` and that one day, and shares the transaction with the
    insert so a failure between them cannot leave the day empty. Scope, not prohibition:
    nothing outside the recomputed vehicle-day is ever touched.
    """
    day_start = datetime(key.day.year, key.day.month, key.day.day, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET ROLE fleet_batch")
            # Bounded three ways: one vehicle, one day, one table per statement. A broader
            # predicate here is the failure mode that erases a fleet's history.
            cur.execute(
                "DELETE FROM idle_events WHERE vin = %s AND started_at >= %s AND started_at < %s",
                (key.vin, day_start, day_end),
            )
            cur.execute(
                "DELETE FROM trips WHERE vin = %s AND started_at >= %s AND started_at < %s",
                (key.vin, day_start, day_end),
            )

        idle_written = idle_writer(segments) if segments else 0
        trips_written = trip_writer(trips) if trips else 0

    return idle_written, trips_written
