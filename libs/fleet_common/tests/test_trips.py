"""Tests for trip segmentation.

The asymmetry with idle detection is the thing to hold onto: idle detection may
under-claim, because an over-stated cost is worse than an under-stated one. Trip
detection may **not** under-claim, because trips are the denominator of the utilisation
score - miss one and a working vehicle looks like an asset to sell.
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fleet_common.domain.trips import (
    TripConfig,
    TripState,
    TripStateMachine,
    segment_trips,
    summarise_day,
)
from fleet_common.events import Ignition, TelemetryEvent
from fleet_common.testing import make_event

CONFIG = TripConfig(
    move_speed_kph=5.0,
    stop_speed_kph=2.0,
    min_stop_seconds=300,
    min_trip_seconds=120,
    min_trip_distance_km=0.5,
    gap_tolerance_seconds=300,
)


def drive(
    *,
    start: float,
    duration: float,
    speed_kph: float = 50.0,
    step: float = 10.0,
    odometer_start: float = 1000.0,
    seq_start: int = 0,
) -> list[TelemetryEvent]:
    """A run of moving samples with a plausibly advancing odometer."""
    count = int(duration // step) + 1
    events = []
    odometer = odometer_start
    for i in range(count):
        events.append(
            make_event(
                offset_seconds=start + i * step,
                speed_kph=speed_kph,
                odometer_km=round(odometer, 3),
                seq=seq_start + i,
            )
        )
        odometer += speed_kph * step / 3600.0
    return events


def stop(
    *,
    start: float,
    duration: float,
    step: float = 10.0,
    odometer: float = 1000.0,
    seq_start: int = 0,
) -> list[TelemetryEvent]:
    """A run of stationary samples; the odometer does not advance."""
    count = int(duration // step) + 1
    return [
        make_event(
            offset_seconds=start + i * step,
            speed_kph=0.0,
            odometer_km=odometer,
            seq=seq_start + i,
        )
        for i in range(count)
    ]


# --------------------------------------------------------------------------
# The behaviour that matters most
# --------------------------------------------------------------------------


def test_a_brief_stop_does_not_end_a_trip() -> None:
    """A traffic light is part of a journey, not the end of one.

    Splitting here would turn one delivery run into fifteen, inflating the trip count
    and destroying the active-day signal utilisation rests on.
    """
    events = [
        *drive(start=0, duration=600, odometer_start=1000.0),
        *stop(start=610, duration=90, odometer=1008.4, seq_start=100),
        *drive(start=710, duration=600, odometer_start=1008.4, seq_start=200),
    ]
    trips = segment_trips(events, CONFIG)
    assert len(trips) == 1, "a 90-second stop split one trip into several"


def test_a_long_stop_ends_the_trip() -> None:
    events = [
        *drive(start=0, duration=600, odometer_start=1000.0),
        *stop(start=610, duration=600, odometer=1008.4, seq_start=100),
        *drive(start=1220, duration=600, odometer_start=1008.4, seq_start=300),
    ]
    trips = segment_trips(events, CONFIG)
    assert len(trips) == 2


def test_the_trip_ends_where_the_vehicle_stopped_not_where_we_noticed() -> None:
    """Counting the whole pause as trip time would inflate duration and drag the
    average speed down for every vehicle that ever parks."""
    events = [
        *drive(start=0, duration=600, odometer_start=1000.0),
        *stop(start=610, duration=900, odometer=1008.4, seq_start=100),
    ]
    trip = segment_trips(events, CONFIG)[0]
    # The pause began at 610s; the trip must not absorb the following 900 seconds.
    assert trip.duration_seconds == pytest.approx(610.0, abs=15.0)


def test_pausing_keeps_the_trip_open() -> None:
    machine = TripStateMachine(CONFIG)
    for event in drive(start=0, duration=300):
        machine.observe(event)
    assert machine.state is TripState.MOVING

    for event in stop(start=310, duration=60, odometer=1004.2, seq_start=100):
        machine.observe(event)
    assert machine.state is TripState.PAUSED, "a brief stop must not close the trip"


# --------------------------------------------------------------------------
# What counts as a trip
# --------------------------------------------------------------------------


def test_manoeuvring_in_a_yard_is_not_a_trip() -> None:
    """Above the move threshold and long enough in time, but it went nowhere.

    200 seconds at 6 kph is about 330 m - shunting between bays, not a journey. The
    distance bound is what rejects it.
    """
    events = drive(start=0, duration=200, speed_kph=6.0, odometer_start=1000.0)
    assert segment_trips(events, CONFIG) == []


def test_a_short_but_real_journey_is_a_trip() -> None:
    """The other side of that line, so the threshold is pinned from both directions.

    600 seconds at 6 kph is 1 km. Slow, but a genuine delivery leg rather than
    manoeuvring, and treating it as nothing would under-count a working vehicle.
    """
    events = drive(start=0, duration=600, speed_kph=6.0, odometer_start=1000.0)
    trips = segment_trips(events, CONFIG)
    assert len(trips) == 1
    assert trips[0].distance_km == pytest.approx(1.0, rel=0.05)


def test_a_very_short_movement_is_not_a_trip() -> None:
    """Far enough, but over in seconds - repositioning, not a journey."""
    events = drive(start=0, duration=60, speed_kph=90.0, odometer_start=1000.0)
    assert segment_trips(events, CONFIG) == []


def test_a_real_journey_is_a_trip() -> None:
    trips = segment_trips(drive(start=0, duration=1800, speed_kph=60.0), CONFIG)
    assert len(trips) == 1
    assert trips[0].distance_km > 25.0


def test_distance_comes_from_the_odometer_not_integrated_fixes() -> None:
    """An odometer is monotonic and does not accumulate the error that summing noisy
    GPS legs would."""
    events = drive(start=0, duration=3600, speed_kph=60.0, odometer_start=5000.0)
    trip = segment_trips(events, CONFIG)[0]
    assert trip.distance_km == pytest.approx(60.0, rel=0.02)


def test_identity_is_stamped_onto_trips() -> None:
    events = drive(start=0, duration=600)
    trip = segment_trips(events, CONFIG)[0]
    assert trip.vin == events[0].vin
    assert trip.tenant_id == events[0].tenant_id


# --------------------------------------------------------------------------
# Hysteresis and gaps
# --------------------------------------------------------------------------


def test_speed_between_the_bounds_does_not_start_a_trip() -> None:
    """From STOPPED the move bound (5 kph) applies, so crawling at 3 kph is not a trip."""
    events = drive(start=0, duration=900, speed_kph=3.0, odometer_start=1000.0)
    assert segment_trips(events, CONFIG) == []


def test_speed_between_the_bounds_keeps_a_trip_alive() -> None:
    """Once moving the stop bound (2 kph) applies, so slowing to 3 kph in traffic does
    not shatter the trip."""
    events = [
        *drive(start=0, duration=300, speed_kph=50.0, odometer_start=1000.0),
        *drive(start=310, duration=100, speed_kph=3.0, odometer_start=1004.2, seq_start=100),
        *drive(start=420, duration=300, speed_kph=50.0, odometer_start=1004.3, seq_start=200),
    ]
    assert len(segment_trips(events, CONFIG)) == 1


def test_config_rejects_a_move_bound_below_the_stop_bound() -> None:
    with pytest.raises(ValueError, match="flaps between moving and stopped"):
        TripConfig(move_speed_kph=2.0, stop_speed_kph=5.0)


def test_a_gap_beyond_tolerance_closes_the_trip() -> None:
    """Beyond the tolerance the odometer may have advanced a long way unseen, so the
    unobserved distance cannot be attributed to this trip."""
    events = [
        *drive(start=0, duration=600, odometer_start=1000.0),
        *drive(start=3600, duration=600, odometer_start=1200.0, seq_start=500),
    ]
    assert len(segment_trips(events, CONFIG)) == 2


# --------------------------------------------------------------------------
# Derived figures
# --------------------------------------------------------------------------


def test_moving_seconds_excludes_paused_time() -> None:
    """Utilisation needs time actually worked, not elapsed time."""
    events = [
        *drive(start=0, duration=600, odometer_start=1000.0),
        *stop(start=610, duration=120, odometer=1008.4, seq_start=100),
        *drive(start=740, duration=600, odometer_start=1008.4, seq_start=200),
    ]
    trip = segment_trips(events, CONFIG)[0]
    assert trip.moving_seconds < trip.duration_seconds
    assert trip.idle_seconds > 0


def test_average_speed_uses_moving_time() -> None:
    """Elapsed time would include pauses, making every urban trip look slow and hiding
    a genuinely slow one."""
    events = [
        *drive(start=0, duration=600, speed_kph=60.0, odometer_start=1000.0),
        *stop(start=610, duration=120, odometer=1010.0, seq_start=100),
        *drive(start=740, duration=600, speed_kph=60.0, odometer_start=1010.0, seq_start=200),
    ]
    trip = segment_trips(events, CONFIG)[0]
    assert trip.average_speed_kph == pytest.approx(60.0, rel=0.25)


def test_average_speed_is_zero_when_no_moving_time_was_recorded() -> None:
    """Guards the divide-by-zero rather than asserting a positive number, which is what
    the previous name promised and the body did not check."""
    from datetime import UTC, datetime

    from fleet_common.domain.trips import Trip

    start = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
    trip = Trip(
        tenant_id="t",
        vin="V",
        started_at=start,
        ended_at=start,
        duration_seconds=0.0,
        moving_seconds=0.0,
        idle_seconds=0.0,
        distance_km=5.0,
        sample_count=1,
        max_gap_seconds=0.0,
        start_lat=None,
        start_lon=None,
        end_lat=None,
        end_lon=None,
    )
    assert trip.average_speed_kph == 0.0


def test_average_speed_is_positive_for_a_real_trip() -> None:
    machine = TripStateMachine(CONFIG)
    for event in drive(start=0, duration=600, speed_kph=60.0):
        machine.observe(event)
    trip = machine.flush()
    assert trip is not None
    assert trip.average_speed_kph > 0


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_flush_closes_a_trip_open_at_shutdown() -> None:
    machine = TripStateMachine(CONFIG)
    for event in drive(start=0, duration=900, speed_kph=60.0):
        machine.observe(event)
    flushed = machine.flush()
    assert flushed is not None
    assert machine.flush() is None, "flush must be idempotent"


def test_out_of_order_event_is_reported_not_applied() -> None:
    machine = TripStateMachine(CONFIG)
    machine.observe(make_event(offset_seconds=100, speed_kph=50.0, odometer_km=1000.0))
    observation = machine.observe(make_event(offset_seconds=50, speed_kph=50.0, odometer_km=999.0))
    assert observation.out_of_order is True


def test_empty_stream_produces_nothing() -> None:
    assert segment_trips([], CONFIG) == []


def test_engine_off_stationary_stream_produces_nothing() -> None:
    events = [
        make_event(offset_seconds=i * 10, speed_kph=0.0, ignition=Ignition.OFF) for i in range(50)
    ]
    assert segment_trips(events, CONFIG) == []


# --------------------------------------------------------------------------
# Daily aggregation, which feeds the utilisation score
# --------------------------------------------------------------------------


def test_daily_summary_aggregates_trips() -> None:
    trips = segment_trips(drive(start=0, duration=1800, speed_kph=60.0), CONFIG)
    summary = summarise_day("VIN1", "2026-09-29", trips, idle_seconds=600.0)
    assert summary.trip_count == 1
    assert summary.distance_km > 25.0
    assert summary.idle_seconds == 600.0
    assert summary.was_active is True


def test_a_day_with_no_trips_is_not_active() -> None:
    """The active-day signal is what surfaces an under-used asset (D3)."""
    summary = summarise_day("VIN1", "2026-09-29", [], idle_seconds=0.0)
    assert summary.was_active is False
    assert summary.trip_count == 0


# --------------------------------------------------------------------------
# Property-based invariants
# --------------------------------------------------------------------------

_sample = st.tuples(
    st.integers(min_value=1, max_value=600),
    st.floats(min_value=0.0, max_value=110.0, allow_nan=False, allow_infinity=False),
)


def _build(samples: list[tuple[int, float]]) -> list[TelemetryEvent]:
    events: list[TelemetryEvent] = []
    offset = 0
    odometer = 1000.0
    for i, (dt, speed) in enumerate(samples):
        offset += dt
        odometer += speed * dt / 3600.0
        events.append(
            make_event(
                offset_seconds=offset,
                speed_kph=speed,
                odometer_km=round(odometer, 3),
                seq=i,
            )
        )
    return events


@given(st.lists(_sample, min_size=0, max_size=120))
@settings(max_examples=250, deadline=None)
def test_property_trips_are_ordered_and_non_overlapping(
    samples: list[tuple[int, float]],
) -> None:
    """Overlapping trips would double-count distance for the same journey."""
    trips = segment_trips(_build(samples), CONFIG)
    for earlier, later in itertools.pairwise(trips):
        assert earlier.ended_at <= later.started_at


@given(st.lists(_sample, min_size=0, max_size=120))
@settings(max_examples=250, deadline=None)
def test_property_every_trip_meets_both_minimums(
    samples: list[tuple[int, float]],
) -> None:
    for trip in segment_trips(_build(samples), CONFIG):
        assert trip.duration_seconds >= CONFIG.min_trip_seconds
        assert trip.distance_km >= CONFIG.min_trip_distance_km


@given(st.lists(_sample, min_size=2, max_size=120))
@settings(max_examples=250, deadline=None)
def test_property_total_trip_distance_never_exceeds_the_odometer_span(
    samples: list[tuple[int, float]],
) -> None:
    """Distance is derived from a monotonic odometer, so the trips cannot together
    claim more ground than the vehicle covered."""
    events = _build(samples)
    span = events[-1].odometer_km - events[0].odometer_km
    total = sum(t.distance_km for t in segment_trips(events, CONFIG))
    assert total <= span + 1e-6


@given(st.lists(_sample, min_size=2, max_size=120))
@settings(max_examples=200, deadline=None)
def test_property_moving_time_never_exceeds_trip_duration(
    samples: list[tuple[int, float]],
) -> None:
    for trip in segment_trips(_build(samples), CONFIG):
        assert trip.moving_seconds <= trip.duration_seconds + 1e-6


@given(st.lists(_sample, min_size=1, max_size=80))
@settings(max_examples=200, deadline=None)
def test_property_replaying_the_stream_is_idempotent(
    samples: list[tuple[int, float]],
) -> None:
    """At-least-once delivery means duplicates are routine."""
    events = _build(samples)
    once = segment_trips(events, CONFIG)
    doubled: list[TelemetryEvent] = []
    for event in events:
        doubled.extend([event, event])
    twice = segment_trips(doubled, CONFIG)
    assert [(t.started_at, t.ended_at) for t in once] == [(t.started_at, t.ended_at) for t in twice]
