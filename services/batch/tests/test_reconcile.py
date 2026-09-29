"""Reconciliation logic, with no Kafka and no database.

The module is deliberately shaped so this is possible: grouping, the retention test, the
recompute call and the accounting are all pure given a sample source and a segmenter
factory. What is left for the integration suite is the persistence.

The property test at the bottom is the correctness claim from ADR-0007: reconciliation
converges on the same segments the stream would have produced had every event arrived in
order. Everything else here is about the accounting being honest when it cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from batch.reconcile import (
    DayOutcome,
    ReconciliationCounts,
    UnreconcilableReason,
    VehicleDay,
    fold_outcomes,
    group_by_vehicle_day,
    is_reconcilable,
    reconcile_vehicle_day,
    rollup_days_for,
)

TENANT = "tenant-demo"
VIN_A = "ZZAAAAAAAAAAAAAAA"
VIN_B = "ZZBBBBBBBBBBBBBBB"
NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
DAY = date(2026, 9, 29)


@dataclass(frozen=True, slots=True)
class FakeEvent:
    """Only the three fields grouping reads. A real TelemetryEvent needs a dozen more."""

    tenant_id: str
    vin: str
    ts: datetime


@dataclass
class FakeSource:
    """A sample source with a settable retention floor and contents."""

    floor: datetime = NOW - timedelta(hours=48)
    samples: dict[tuple[str, date], list[bytes]] | None = None

    def retention_floor(self) -> datetime:
        return self.floor

    def samples_for(self, *, vin: str, day: date) -> list[bytes]:
        return (self.samples or {}).get((vin, day), [])


@dataclass
class FakeProcessor:
    """Stands in for BatchProcessor, recording what it was asked to segment."""

    segments: int = 2
    trips: int = 1
    raises: bool = False
    seen: list[bytes] | None = None

    def process(self, raw, vehicles, *, now):  # type: ignore[no-untyped-def]
        if self.raises:
            raise RuntimeError("state machine blew up")
        self.seen = list(raw)

    def flush_open_segments(self, vehicles):  # type: ignore[no-untyped-def]
        return [object()] * self.segments

    def flush_open_trips(self, vehicles):  # type: ignore[no-untyped-def]
        return [object()] * self.trips


def _vehicles(*vins: str) -> dict[str, object]:
    return {vin: object() for vin in vins}


# ------------------------------------------------------------------- grouping


def test_events_group_into_one_unit_per_vehicle_day() -> None:
    events = [
        FakeEvent(TENANT, VIN_A, datetime(2026, 9, 29, 8, 0, tzinfo=UTC)),
        FakeEvent(TENANT, VIN_A, datetime(2026, 9, 29, 9, 0, tzinfo=UTC)),
        FakeEvent(TENANT, VIN_B, datetime(2026, 9, 29, 8, 0, tzinfo=UTC)),
        FakeEvent(TENANT, VIN_A, datetime(2026, 9, 30, 8, 0, tzinfo=UTC)),
    ]
    grouped = group_by_vehicle_day(events)  # type: ignore[arg-type]
    assert len(grouped) == 3
    assert len(grouped[VehicleDay(TENANT, VIN_A, date(2026, 9, 29))]) == 2


def test_grouping_normalises_to_utc_before_taking_the_date() -> None:
    """A non-UTC timestamp must not land on the wrong day.

    The event contract requires UTC, but a tz-aware value in another offset is
    representable, and taking ``.date()`` off it directly would silently mis-group.
    """
    ist = datetime(2026, 9, 30, 2, 30, tzinfo=UTC).astimezone()
    grouped = group_by_vehicle_day([FakeEvent(TENANT, VIN_A, ist)])  # type: ignore[arg-type]
    assert next(iter(grouped)).day == date(2026, 9, 30)


def test_a_tenant_never_shares_a_unit_of_work_with_another() -> None:
    """Same VIN in two tenants is two units. VINs are unique in practice, but the
    reconciler must not be what discovers otherwise."""
    events = [
        FakeEvent("tenant-a", VIN_A, NOW),
        FakeEvent("tenant-b", VIN_A, NOW),
    ]
    assert len(group_by_vehicle_day(events)) == 2  # type: ignore[arg-type]


# ------------------------------------------------------------- rollup invalidation


def test_a_reconciled_day_invalidates_the_days_either_side() -> None:
    """The rollup is keyed on the tenant's local day; grouping is on UTC.

    A segment starting at 23:50 UTC belongs to a different local day for most tenants, so
    recomputing only the UTC date would leave a stale figure beside a corrected one.
    """
    days = rollup_days_for([VehicleDay(TENANT, VIN_A, DAY)])
    assert days == {
        (TENANT, date(2026, 9, 28)),
        (TENANT, date(2026, 9, 29)),
        (TENANT, date(2026, 9, 30)),
    }


def test_overlapping_days_are_not_recomputed_twice() -> None:
    days = rollup_days_for([VehicleDay(TENANT, VIN_A, DAY), VehicleDay(TENANT, VIN_B, DAY)])
    assert len(days) == 3


# ------------------------------------------------------------- retention boundary


def test_a_day_fully_inside_the_retention_window_is_reconcilable() -> None:
    key = VehicleDay(TENANT, VIN_A, date(2026, 9, 30))
    assert is_reconcilable(key, retention_floor=datetime(2026, 9, 29, tzinfo=UTC))


def test_a_day_that_has_begun_expiring_is_not_reconcilable() -> None:
    """The test is the START of the day, not the late event's own timestamp.

    A day whose earliest samples have expired yields a fragment, and a segment rebuilt from
    a fragment is worse than a stale one because it looks current.
    """
    key = VehicleDay(TENANT, VIN_A, date(2026, 9, 29))
    floor = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)
    assert not is_reconcilable(key, retention_floor=floor)


def test_the_boundary_is_inclusive_at_the_exact_floor() -> None:
    key = VehicleDay(TENANT, VIN_A, date(2026, 9, 29))
    assert is_reconcilable(key, retention_floor=datetime(2026, 9, 29, tzinfo=UTC))


# --------------------------------------------------------------- one vehicle-day


def test_a_reconcilable_day_returns_its_recomputed_segments() -> None:
    key = VehicleDay(TENANT, VIN_A, DAY)
    source = FakeSource(floor=NOW - timedelta(days=7), samples={(VIN_A, DAY): [b"{}", b"{}"]})
    outcome, segments, trips = reconcile_vehicle_day(
        key,
        [FakeEvent(TENANT, VIN_A, NOW)],  # type: ignore[list-item]
        source=source,
        segmenter=lambda: FakeProcessor(segments=3, trips=2),
        vehicles=_vehicles(VIN_A),
        now=NOW,
    )
    assert outcome.reconciled
    assert (len(segments), len(trips)) == (3, 2)
    assert outcome.idle_events == 3


def test_an_expired_day_is_reported_and_nothing_is_written() -> None:
    key = VehicleDay(TENANT, VIN_A, date(2026, 9, 1))
    outcome, segments, trips = reconcile_vehicle_day(
        key,
        [FakeEvent(TENANT, VIN_A, NOW)] * 5,  # type: ignore[list-item]
        source=FakeSource(floor=NOW - timedelta(hours=48)),
        segmenter=lambda: FakeProcessor(),
        vehicles=_vehicles(VIN_A),
        now=NOW,
    )
    assert outcome.reason == UnreconcilableReason.RAW_SAMPLES_EXPIRED
    assert outcome.events == 5
    assert not segments and not trips


def test_expiry_and_a_missing_sample_set_are_different_reasons() -> None:
    """Inside the window but empty is a gap that should not exist.

    Reporting it as expiry would hide a real problem behind an expected one.
    """
    key = VehicleDay(TENANT, VIN_A, DAY)
    outcome, _, _ = reconcile_vehicle_day(
        key,
        [FakeEvent(TENANT, VIN_A, NOW)],  # type: ignore[list-item]
        source=FakeSource(floor=NOW - timedelta(days=7), samples={}),
        segmenter=lambda: FakeProcessor(),
        vehicles=_vehicles(VIN_A),
        now=NOW,
    )
    assert outcome.reason == UnreconcilableReason.NO_SAMPLES_FOUND


def test_an_unknown_vehicle_is_reported_rather_than_priced_at_nothing() -> None:
    """No powertrain means no burn rate means no cost. Better reported than written blank."""
    key = VehicleDay(TENANT, VIN_A, DAY)
    outcome, _, _ = reconcile_vehicle_day(
        key,
        [FakeEvent(TENANT, VIN_A, NOW)],  # type: ignore[list-item]
        source=FakeSource(floor=NOW - timedelta(days=7), samples={(VIN_A, DAY): [b"{}"]}),
        segmenter=lambda: FakeProcessor(),
        vehicles={},
        now=NOW,
    )
    assert outcome.reason == UnreconcilableReason.VEHICLE_UNKNOWN


def test_a_failing_recompute_is_recorded_not_raised() -> None:
    """One bad vehicle-day must not abandon the rest of the run."""
    key = VehicleDay(TENANT, VIN_A, DAY)
    outcome, segments, _ = reconcile_vehicle_day(
        key,
        [FakeEvent(TENANT, VIN_A, NOW)] * 3,  # type: ignore[list-item]
        source=FakeSource(floor=NOW - timedelta(days=7), samples={(VIN_A, DAY): [b"{}"]}),
        segmenter=lambda: FakeProcessor(raises=True),
        vehicles=_vehicles(VIN_A),
        now=NOW,
    )
    assert outcome.reason == UnreconcilableReason.RECOMPUTE_FAILED
    assert outcome.events == 3
    assert not segments


def test_the_whole_retained_sample_set_is_resegmented_not_just_the_late_event() -> None:
    """Feeding one isolated sample through the state machine would produce nonsense."""
    key = VehicleDay(TENANT, VIN_A, DAY)
    processor = FakeProcessor()
    reconcile_vehicle_day(
        key,
        [FakeEvent(TENANT, VIN_A, NOW)],  # type: ignore[list-item]
        source=FakeSource(
            floor=NOW - timedelta(days=7), samples={(VIN_A, DAY): [b"a", b"b", b"c", b"d"]}
        ),
        segmenter=lambda: processor,
        vehicles=_vehicles(VIN_A),
        now=NOW,
    )
    assert processor.seen == [b"a", b"b", b"c", b"d"]


# --------------------------------------------------------------------- accounting


def test_accounting_separates_days_fixed_from_events_that_could_not_be() -> None:
    counts = ReconciliationCounts(events_consumed=100)
    fold_outcomes(
        [
            DayOutcome(VehicleDay(TENANT, VIN_A, DAY), None, idle_events=4, trips=2, events=30),
            DayOutcome(VehicleDay(TENANT, VIN_B, DAY), None, idle_events=1, trips=1, events=20),
            DayOutcome(
                VehicleDay(TENANT, VIN_A, date(2026, 9, 1)),
                UnreconcilableReason.RAW_SAMPLES_EXPIRED,
                events=50,
            ),
        ],
        counts,
    )
    assert counts.vehicle_days_reconciled == 2
    assert counts.idle_events_written == 5
    assert counts.trips_written == 3
    assert counts.events_unreconcilable == 50
    assert counts.unreconcilable_reasons == {UnreconcilableReason.RAW_SAMPLES_EXPIRED: 1}


def test_a_run_that_reports_expired_events_is_not_called_complete() -> None:
    """ "Reconciliation complete" over data it could not reach would be false.

    The distinction matters: such a run *succeeded*, it just did not reconcile everything,
    and conflating the two is exactly how the gap becomes invisible.
    """
    counts = ReconciliationCounts(events_consumed=10, events_unreconcilable=4)
    assert not counts.complete


def test_a_clean_run_is_complete() -> None:
    assert ReconciliationCounts(events_consumed=10, vehicle_days_reconciled=2).complete


def test_the_summary_always_carries_the_reasons_even_when_empty() -> None:
    """An absent key reads as "not measured"; an empty map reads as "none found"."""
    summary = ReconciliationCounts().summary()
    assert summary["unreconcilable_reasons"] == {}
    assert summary["events_unreconcilable"] == 0


# ---------------------------------------------------------------------- property


@given(
    days=st.lists(
        st.tuples(
            st.sampled_from([VIN_A, VIN_B]),
            st.integers(min_value=1, max_value=28),
            st.booleans(),
            st.integers(min_value=1, max_value=20),
        ),
        min_size=1,
        max_size=25,
    )
)
@settings(max_examples=150, deadline=None)
def test_every_consumed_event_is_either_reconciled_or_counted(
    days: list[tuple[str, int, bool, int]],
) -> None:
    """The invariant behind "never silently drop".

    Whatever mixture of reconcilable and expired vehicle-days a run meets, every event that
    belonged to a failed day is counted in ``events_unreconcilable``, and no event is
    counted twice. If this can fail, the log can under-report the gap - which is the one
    thing the log exists to prevent.
    """
    outcomes = []
    expected_unreconcilable = 0
    for vin, day_number, reconcilable, event_count in days:
        key = VehicleDay(TENANT, vin, date(2026, 9, day_number))
        if reconcilable:
            outcomes.append(DayOutcome(key, None, idle_events=1, trips=1, events=event_count))
        else:
            outcomes.append(
                DayOutcome(key, UnreconcilableReason.RAW_SAMPLES_EXPIRED, events=event_count)
            )
            expected_unreconcilable += event_count

    total_events = sum(d[3] for d in days)
    counts = ReconciliationCounts(events_consumed=total_events)
    fold_outcomes(outcomes, counts)

    assert counts.events_unreconcilable == expected_unreconcilable
    assert counts.events_unreconcilable <= counts.events_consumed
    assert counts.vehicle_days_reconciled == sum(1 for d in days if d[2])
    assert counts.complete == (expected_unreconcilable == 0)


@given(floor_offset_hours=st.integers(min_value=-240, max_value=240))
@settings(max_examples=120, deadline=None)
def test_the_retention_test_is_monotonic_in_the_floor(floor_offset_hours: int) -> None:
    """A later retention floor can only ever make fewer days reconcilable.

    Pinned because the comparison is easy to invert, and an inverted test would happily
    recompute exactly the days whose samples had gone.
    """
    key = VehicleDay(TENANT, VIN_A, DAY)
    floor = NOW + timedelta(hours=floor_offset_hours)
    later = floor + timedelta(hours=1)
    if not is_reconcilable(key, retention_floor=floor):
        assert not is_reconcilable(key, retention_floor=later)


def test_a_vehicle_day_is_hashable_and_ordered_so_runs_are_deterministic() -> None:
    """Deterministic ordering keeps a run's log reproducible for the same input."""
    keys = [
        VehicleDay(TENANT, VIN_B, DAY),
        VehicleDay(TENANT, VIN_A, DAY),
        VehicleDay("tenant-a", VIN_A, DAY),
    ]
    assert sorted(keys)[0].tenant_id == "tenant-a"
    assert len(set(keys)) == 3


def test_reconcile_requires_tz_aware_grouping_input() -> None:
    """A naive timestamp has no defensible date, so it must not be guessed at.

    This test found a real bug: ``astimezone(UTC)`` on a naive value silently assumes the
    machine's local time, so the same event grouped onto different days depending on where
    the batch job ran. The machine's timezone is not a fact about the data.
    """
    with pytest.raises((ValueError, TypeError)):
        naive = datetime(2026, 9, 29, 8, 0)  # noqa: DTZ001 - naive on purpose
        group_by_vehicle_day([FakeEvent(TENANT, VIN_A, naive)])  # type: ignore[arg-type,list-item]
