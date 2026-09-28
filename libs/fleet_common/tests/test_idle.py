"""Tests for idle segmentation (D1).

Covers the two behaviours that make this algorithm non-trivial - hysteresis and
gap tolerance - plus the edge cases CLAUDE.md S12 calls out: duplicates,
out-of-order samples and boundary durations. Property-based tests assert
invariants that must hold for *any* input stream, not just the cases we thought of.
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from fleet_common.domain.idle import (
    IdleConfig,
    IdleState,
    IdleStateMachine,
    segment_idle,
)
from fleet_common.events import Ignition
from fleet_common.testing import T0
from fleet_common.testing import make_event as event
from fleet_common.testing import make_idle_run as idle_run

CONFIG = IdleConfig(
    min_seconds=180,
    entry_speed_kph=2.0,
    exit_speed_kph=5.0,
    excessive_seconds=600,
    gap_tolerance_seconds=30,
)


# --------------------------------------------------------------------------
# Threshold behaviour
# --------------------------------------------------------------------------


def test_run_below_min_seconds_produces_no_segment() -> None:
    """Short idling is unavoidable (lights, loading) and is not billed (D1)."""
    events = idle_run(start=0, duration=170, step=10)
    assert segment_idle(events, CONFIG) == []


def test_run_at_exactly_min_seconds_produces_one_segment() -> None:
    """The threshold is inclusive: 180s of observed idle is billable."""
    segments = segment_idle(idle_run(start=0, duration=180, step=10), CONFIG)
    assert len(segments) == 1
    assert segments[0].duration_seconds == pytest.approx(180.0)


def test_segment_duration_spans_first_to_last_observed_sample() -> None:
    """Duration never counts unobserved time, so the estimate under-claims."""
    segments = segment_idle(idle_run(start=0, duration=300, step=10), CONFIG)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.started_at == T0
    assert seg.ended_at == T0 + timedelta(seconds=300)
    assert seg.sample_count == 31


def test_moving_then_idle_then_moving_yields_one_segment() -> None:
    events = [
        event(offset_seconds=0, speed_kph=50.0),
        *idle_run(start=10, duration=240, step=10),
        event(offset_seconds=260, speed_kph=40.0),
    ]
    segments = segment_idle(events, CONFIG)
    assert len(segments) == 1
    # Closing on the moving sample extends the segment to that instant.
    assert segments[0].ended_at == T0 + timedelta(seconds=260)


def test_identity_is_stamped_onto_segments() -> None:
    events = idle_run(start=0, duration=200)
    seg = segment_idle(events, CONFIG)[0]
    assert seg.vin == events[0].vin
    assert seg.tenant_id == events[0].tenant_id


# --------------------------------------------------------------------------
# Hysteresis: the reason entry and exit bounds differ
# --------------------------------------------------------------------------


def test_speed_between_bounds_does_not_start_a_segment() -> None:
    """From MOVING the entry bound (2.0) applies, so 3 kph is not idle."""
    events = [event(offset_seconds=i * 10, speed_kph=3.0) for i in range(40)]
    assert segment_idle(events, CONFIG) == []


def test_speed_between_bounds_keeps_an_open_segment_alive() -> None:
    """Once idling the exit bound (5.0) applies, so GPS jitter to 4 kph does not
    shatter one real idle period into several short ones."""
    events = [
        *idle_run(start=0, duration=100, step=10),
        event(offset_seconds=110, speed_kph=4.0),  # jitter, still idle
        *[event(offset_seconds=120 + i * 10, speed_kph=0.0) for i in range(9)],
    ]
    segments = segment_idle(events, CONFIG)
    assert len(segments) == 1, "jitter inside the hysteresis band must not split the segment"
    assert segments[0].duration_seconds == pytest.approx(200.0)


def test_speed_above_exit_bound_closes_the_segment() -> None:
    events = [
        *idle_run(start=0, duration=200, step=10),
        event(offset_seconds=210, speed_kph=6.0),
    ]
    segments = segment_idle(events, CONFIG)
    assert len(segments) == 1
    assert segments[0].ended_at == T0 + timedelta(seconds=210)


def test_config_rejects_exit_bound_below_entry_bound() -> None:
    """Without this guard the machine would flap on sensor noise."""
    with pytest.raises(ValueError, match="flaps on sensor noise"):
        IdleConfig(entry_speed_kph=5.0, exit_speed_kph=2.0)


# --------------------------------------------------------------------------
# Gap tolerance
# --------------------------------------------------------------------------


def test_gap_within_tolerance_is_bridged() -> None:
    """A dropped uplink must not split one real idle period."""
    events = [
        *idle_run(start=0, duration=100, step=10),
        # 25s gap: inside the 30s tolerance.
        *[event(offset_seconds=125 + i * 10, speed_kph=0.0) for i in range(10)],
    ]
    segments = segment_idle(events, CONFIG)
    assert len(segments) == 1
    assert segments[0].max_gap_seconds == pytest.approx(25.0)


def test_gap_beyond_tolerance_closes_and_restarts() -> None:
    """Beyond tolerance we cannot vouch for unobserved time, so we split."""
    events = [
        *idle_run(start=0, duration=200, step=10),
        # 600s gap: far outside tolerance.
        *[event(offset_seconds=800 + i * 10, speed_kph=0.0) for i in range(30)],
    ]
    segments = segment_idle(events, CONFIG)
    assert len(segments) == 2
    assert segments[0].ended_at == T0 + timedelta(seconds=200)
    assert segments[1].started_at == T0 + timedelta(seconds=800)


def test_gap_beyond_tolerance_discards_a_sub_threshold_first_segment() -> None:
    events = [
        *idle_run(start=0, duration=60, step=10),
        *[event(offset_seconds=700 + i * 10, speed_kph=0.0) for i in range(30)],
    ]
    segments = segment_idle(events, CONFIG)
    assert len(segments) == 1
    assert segments[0].started_at == T0 + timedelta(seconds=700)


def test_max_gap_seconds_is_reported_for_data_quality() -> None:
    """A segment resting on sparse samples must be visible as such."""
    events = [
        *idle_run(start=0, duration=100, step=10),
        *[event(offset_seconds=128 + i * 10, speed_kph=0.0) for i in range(10)],
    ]
    seg = segment_idle(events, CONFIG)[0]
    assert seg.max_gap_seconds == pytest.approx(28.0)


# --------------------------------------------------------------------------
# Engine state gating
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ignition", [Ignition.OFF, Ignition.ACCESSORY])
def test_unpowered_engine_is_never_idle(ignition: Ignition) -> None:
    """Parked with the engine off burns no fuel, so it is not idle waste."""
    events = [event(offset_seconds=i * 10, speed_kph=0.0, ignition=ignition) for i in range(40)]
    assert segment_idle(events, CONFIG) == []


def test_pto_exclusion_when_enabled() -> None:
    """A running reefer or crane is productive work, not waste (D1, optional)."""
    events = [event(offset_seconds=i * 10, speed_kph=0.0, pto_active=True) for i in range(40)]
    assert segment_idle(events, IdleConfig(exclude_pto_active=True)) == []
    # With the exclusion off (the default), the same stream is billable idle.
    assert len(segment_idle(events, CONFIG)) == 1


def test_pto_flag_is_recorded_even_when_not_excluded() -> None:
    events = [event(offset_seconds=i * 10, speed_kph=0.0, pto_active=(i == 5)) for i in range(40)]
    assert segment_idle(events, CONFIG)[0].pto_active_any is True


# --------------------------------------------------------------------------
# Excessive-idle alert transition (D10)
# --------------------------------------------------------------------------


def test_excessive_fires_exactly_once_per_segment() -> None:
    """Without once-only semantics a long idle would storm alerts."""
    machine = IdleStateMachine(CONFIG)
    transitions = [
        machine.observe(e).became_excessive for e in idle_run(start=0, duration=1800, step=10)
    ]
    assert sum(transitions) == 1, "excessive-idle must fire once, not on every sample"


def test_excessive_fires_on_the_sample_that_crosses_the_threshold() -> None:
    machine = IdleStateMachine(CONFIG)
    fired_at: float | None = None
    for e in idle_run(start=0, duration=900, step=10):
        if machine.observe(e).became_excessive:
            fired_at = (e.ts - T0).total_seconds()
    assert fired_at == pytest.approx(600.0)


def test_segment_below_excessive_threshold_is_not_flagged() -> None:
    seg = segment_idle(idle_run(start=0, duration=300, step=10), CONFIG)[0]
    assert seg.excessive is False


def test_segment_at_excessive_threshold_is_flagged() -> None:
    seg = segment_idle(idle_run(start=0, duration=600, step=10), CONFIG)[0]
    assert seg.excessive is True


# --------------------------------------------------------------------------
# Ordering, duplicates and lifecycle
# --------------------------------------------------------------------------


def test_out_of_order_event_is_reported_not_applied() -> None:
    """Late data is routed to the late topic by the caller, never dropped silently."""
    machine = IdleStateMachine(CONFIG)
    machine.observe(event(offset_seconds=100, speed_kph=0.0))
    observation = machine.observe(event(offset_seconds=50, speed_kph=0.0))
    assert observation.out_of_order is True
    assert observation.closed_segment is None


def test_duplicate_timestamp_is_treated_as_out_of_order() -> None:
    """A replayed event must not inflate sample counts or durations."""
    machine = IdleStateMachine(CONFIG)
    first = event(offset_seconds=100, speed_kph=0.0)
    machine.observe(first)
    assert machine.observe(first).out_of_order is True


def test_flush_closes_an_open_segment() -> None:
    """Without flush, a segment open at shutdown would be lost."""
    machine = IdleStateMachine(CONFIG)
    for e in idle_run(start=0, duration=400, step=10):
        machine.observe(e)
    assert machine.state is IdleState.CONFIRMED
    flushed = machine.flush()
    assert flushed is not None
    assert flushed.duration_seconds == pytest.approx(400.0)
    assert machine.flush() is None, "flush must be idempotent"


def test_flush_discards_a_sub_threshold_open_segment() -> None:
    machine = IdleStateMachine(CONFIG)
    for e in idle_run(start=0, duration=60, step=10):
        machine.observe(e)
    assert machine.flush() is None


def test_empty_stream_produces_nothing() -> None:
    assert segment_idle([], CONFIG) == []


def test_fuel_rate_samples_are_collected_for_the_cross_check() -> None:
    """Feeds the D2 measured cross-check."""
    events = idle_run(start=0, duration=200, step=10, fuel_rate_lph=2.5)
    seg = segment_idle(events, CONFIG)[0]
    assert len(seg.fuel_rate_samples) == 21
    assert all(r == pytest.approx(2.5) for r in seg.fuel_rate_samples)


# --------------------------------------------------------------------------
# Property-based invariants (CLAUDE.md S12: Hypothesis for algorithms)
# --------------------------------------------------------------------------

#: One sample: seconds since the previous sample, speed, and engine-powered flag.
_sample = st.tuples(
    st.integers(min_value=1, max_value=400),
    st.floats(min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False),
    st.booleans(),
)


def _build(samples: list[tuple[int, float, bool]]) -> list:
    events = []
    offset = 0
    for i, (dt, speed, powered) in enumerate(samples):
        offset += dt
        events.append(
            event(
                offset_seconds=offset,
                speed_kph=speed,
                ignition=Ignition.ON if powered else Ignition.OFF,
                seq=i,
            )
        )
    return events


@given(st.lists(_sample, min_size=0, max_size=120))
@settings(max_examples=250, deadline=None)
def test_property_no_segment_is_shorter_than_the_threshold(
    samples: list[tuple[int, float, bool]],
) -> None:
    """Whatever the stream, a billed segment always meets the threshold."""
    for seg in segment_idle(_build(samples), CONFIG):
        assert seg.duration_seconds >= CONFIG.min_seconds


@given(st.lists(_sample, min_size=2, max_size=120))
@settings(max_examples=250, deadline=None)
def test_property_segments_are_ordered_and_non_overlapping(
    samples: list[tuple[int, float, bool]],
) -> None:
    """Overlapping segments would double-count fuel for the same seconds."""
    segments = segment_idle(_build(samples), CONFIG)
    for earlier, later in itertools.pairwise(segments):
        assert earlier.ended_at <= later.started_at


@given(st.lists(_sample, min_size=2, max_size=120))
@settings(max_examples=250, deadline=None)
def test_property_total_idle_never_exceeds_the_observed_span(
    samples: list[tuple[int, float, bool]],
) -> None:
    """The conservative-by-construction guarantee that keeps cost figures honest."""
    events = _build(samples)
    assume(len(events) >= 2)
    span = (events[-1].ts - events[0].ts).total_seconds()
    total = sum(s.duration_seconds for s in segment_idle(events, CONFIG))
    assert total <= span + 1e-6


@given(st.lists(_sample, min_size=1, max_size=120))
@settings(max_examples=200, deadline=None)
def test_property_every_segment_lies_within_the_stream_bounds(
    samples: list[tuple[int, float, bool]],
) -> None:
    events = _build(samples)
    first, last = events[0].ts, events[-1].ts
    for seg in segment_idle(events, CONFIG):
        assert first <= seg.started_at <= seg.ended_at <= last


@given(st.lists(_sample, min_size=1, max_size=80))
@settings(max_examples=200, deadline=None)
def test_property_replaying_the_stream_is_idempotent(
    samples: list[tuple[int, float, bool]],
) -> None:
    """Feeding every event twice must not change the result.

    At-least-once delivery means duplicates are normal, so the algorithm itself
    has to be duplicate-safe rather than relying only on the dedup layer.
    """
    events = _build(samples)
    once = segment_idle(events, CONFIG)
    doubled: list = []
    for e in events:
        doubled.extend([e, e])
    twice = segment_idle(doubled, CONFIG)
    assert [(s.started_at, s.ended_at) for s in once] == [(s.started_at, s.ended_at) for s in twice]
