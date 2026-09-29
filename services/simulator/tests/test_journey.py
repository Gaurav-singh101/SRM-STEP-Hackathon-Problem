"""Tests for journey generation.

The most valuable test here is ``test_idle_detector_finds_the_idle_the_simulator_says
_it_produced``: it feeds generated telemetry through the real idle state machine and
checks the detector agrees with the generator's own ground truth. That closes the
loop between the two halves of the product. Everything else could pass while the
system still measured nothing real.
"""

from __future__ import annotations

import numpy as np
import orjson
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fleet_common.domain.idle import IdleConfig, segment_idle
from fleet_common.events import Ignition, Powertrain, TelemetryEvent
from simulator.fleet import DutyCycle, SyntheticVehicle, build_fleet
from simulator.journey import JourneyConfig, generate_day
from simulator.testing import DAY_START, generate_for

IDLE_CONFIG = IdleConfig(
    min_seconds=180,
    entry_speed_kph=2.0,
    exit_speed_kph=5.0,
    excessive_seconds=600,
    gap_tolerance_seconds=30,
)

#: Fault injection off, so a test about idle detection is not also a test about
#: dropouts and duplicates. Each concern gets its own test.
CLEAN = JourneyConfig(
    duplicate_rate=0.0,
    out_of_order_rate=0.0,
    malformed_rate=0.0,
    dropout_rate=0.0,
    gps_invalid_rate=0.0,
    dtc_rate=0.0,
)


# --------------------------------------------------------------------------
# The loop-closing test
# --------------------------------------------------------------------------


def test_idle_detector_finds_the_idle_the_simulator_says_it_produced() -> None:
    """The generator and the detector must agree, or nothing downstream means anything.

    Compared in aggregate across many vehicles rather than per vehicle: the
    generator counts an idle period by its intended duration, while the detector
    measures observed samples and closes a segment on the first moving sample, so
    individual boundaries differ by a sample interval. A systematic disagreement
    would still show up clearly in the totals.
    """
    fleet = build_fleet(vehicle_count=120, seed=101)
    rng = np.random.default_rng(101)

    expected_billable = 0
    detected_billable = 0

    for vehicle in fleet:
        events, summary = generate_day(vehicle, DAY_START, rng=rng, config=CLEAN)
        expected_billable += summary.billable_idle_periods

        parsed = [TelemetryEvent.model_validate(e) for e in events]
        detected_billable += len(segment_idle(parsed, IDLE_CONFIG))

    assert expected_billable > 50, "test fleet produced too little idle to be meaningful"
    # Within 20%: the two count slightly different things, as explained above.
    ratio = detected_billable / expected_billable
    assert 0.8 <= ratio <= 1.2, (
        f"detector found {detected_billable} billable idle periods, generator "
        f"reported {expected_billable} (ratio {ratio:.2f})"
    )


def test_excessive_idle_is_detectable() -> None:
    """The EXCESSIVE_IDLE_IN_PROGRESS alert (D10) needs these to exist in the data."""
    fleet = build_fleet(vehicle_count=150, seed=102)
    rng = np.random.default_rng(102)

    generated = detected = 0
    for vehicle in fleet:
        events, summary = generate_day(vehicle, DAY_START, rng=rng, config=CLEAN)
        generated += summary.excessive_idle_periods
        parsed = [TelemetryEvent.model_validate(e) for e in events]
        detected += sum(1 for s in segment_idle(parsed, IDLE_CONFIG) if s.excessive)

    assert generated > 0, "no excessive idle generated: the alert path is undemonstrated"
    assert detected > 0, "excessive idle was generated but the detector found none"


def test_stationary_jitter_does_not_shatter_segments() -> None:
    """The simulator emits GPS jitter at rest, which is exactly what hysteresis
    exists for (ADR-0001). If it shattered segments, detected counts would far
    exceed generated ones."""
    vehicle = build_fleet(vehicle_count=1, seed=103)[0]
    events, summary = generate_day(vehicle, DAY_START, rng=np.random.default_rng(103), config=CLEAN)
    parsed = [TelemetryEvent.model_validate(e) for e in events]
    detected = segment_idle(parsed, IDLE_CONFIG)
    # Generous bound; the point is to catch shattering, not to pin an exact count.
    assert len(detected) <= summary.idle_periods + 2


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_same_seed_produces_identical_events() -> None:
    vehicle = build_fleet(vehicle_count=1, seed=1)[0]
    first, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(5))
    second, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(5))
    assert orjson.dumps(first) == orjson.dumps(second)


def test_event_ids_are_deterministic() -> None:
    """uuid4() would make a rerun produce different ids, so a failure could not be
    reproduced and dedup behaviour could not be compared between runs."""
    vehicle = build_fleet(vehicle_count=1, seed=2)[0]
    first, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(6))
    second, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(6))
    assert [e["event_id"] for e in first] == [e["event_id"] for e in second]


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------


def test_clean_events_all_satisfy_the_contract() -> None:
    """With fault injection off, every event must parse. Otherwise the simulator
    would be filling the DLQ with its own defects rather than intended ones."""
    events = generate_for(seed=7, vehicles=5, config=CLEAN)
    assert events
    for event in events:
        TelemetryEvent.model_validate(event)


def test_malformed_events_are_rejected_by_the_contract() -> None:
    """The injected faults must be genuinely invalid, or the DLQ path is untested."""
    config = JourneyConfig(
        malformed_rate=1.0,
        duplicate_rate=0.0,
        out_of_order_rate=0.0,
        dropout_rate=0.0,
    )
    events = generate_for(seed=8, vehicles=2, config=config)
    rejected = 0
    for event in events:
        try:
            TelemetryEvent.model_validate(event)
        except Exception:
            rejected += 1
    assert rejected == len(events), "malformed injection produced valid events"


def test_events_serialise_with_orjson() -> None:
    """The producer hot path uses orjson directly, bypassing Pydantic for speed
    (ADR-0005). Anything unserialisable would fail only at runtime."""
    for event in generate_for(seed=9, vehicles=3, config=CLEAN):
        assert orjson.dumps(event)


def test_timestamps_are_utc_with_a_z_suffix() -> None:
    for event in generate_for(seed=10, vehicles=2, config=CLEAN)[:200]:
        assert event["ts"].endswith("Z"), event["ts"]


def test_sequence_numbers_are_monotonic_per_vehicle() -> None:
    """seq drives the O(1) per-vehicle high-water-mark dedup (ADR-0004), which needs
    monotonicity to be sound."""
    vehicle = build_fleet(vehicle_count=1, seed=11)[0]
    events, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(11), config=CLEAN)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_odometer_never_decreases() -> None:
    """A decreasing odometer is a data-quality signal, so the simulator must not
    emit one by accident."""
    vehicle = build_fleet(vehicle_count=1, seed=12)[0]
    events, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(12), config=CLEAN)
    readings = [e["odometer_km"] for e in events]
    assert readings == sorted(readings)


# --------------------------------------------------------------------------
# Powertrain-specific signals (D9)
# --------------------------------------------------------------------------


def _vehicle_with(powertrain: Powertrain, seed: int = 20) -> SyntheticVehicle:
    for vehicle in build_fleet(vehicle_count=3_000, seed=seed):
        if vehicle.powertrain is powertrain:
            return vehicle
    pytest.skip(f"no {powertrain} vehicle in the generated roster")


def test_bev_reports_state_of_charge_not_fuel() -> None:
    """A BEV emitting a fuel level would contradict the vehicle master and could
    pick up a fuel cost (D9)."""
    vehicle = _vehicle_with(Powertrain.BEV)
    events, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(21), config=CLEAN)
    assert events
    for event in events:
        assert event["fuel_level_pct"] is None
        assert event["fuel_rate_lph"] is None
        assert event["soc_pct"] is not None


def test_combustion_vehicle_reports_fuel_not_state_of_charge() -> None:
    vehicle = _vehicle_with(Powertrain.DIESEL)
    events, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(22), config=CLEAN)
    assert events
    for event in events:
        assert event["fuel_level_pct"] is not None
        assert event["soc_pct"] is None


def _idling_samples_for_a_combustion_vehicle(
    seed: int, *, scan: int = 40
) -> list[dict[str, object]]:
    """Find idling samples from any combustion vehicle in the roster.

    Scans rather than taking the first match: an individual vehicle may legitimately
    produce no idling in a day - an under-used asset often makes no trips at all - so
    asserting on one arbitrary vehicle is a flaky test rather than a real check.
    """
    rng = np.random.default_rng(seed)
    for vehicle in build_fleet(vehicle_count=3_000, seed=seed):
        if not vehicle.powertrain.burns_fuel:
            continue
        events, _ = generate_day(vehicle, DAY_START, rng=rng, config=CLEAN)
        idling = [
            e for e in events if e["ignition"] == Ignition.ON.value and float(e["speed_kph"]) < 2.0
        ]
        if idling:
            return idling
        scan -= 1
        if scan <= 0:
            break
    pytest.fail("no combustion vehicle in the scanned roster produced idling samples")


def test_idling_vehicle_reports_a_non_zero_fuel_rate() -> None:
    """Feeds the D2 measured cross-check. A zero rate while idling would make the
    cross-check falsely contradict the configured burn rate."""
    idling = _idling_samples_for_a_combustion_vehicle(seed=23)
    assert all(float(e["fuel_rate_lph"]) > 0 for e in idling)


def test_reported_idle_rate_matches_the_configured_burn_rate() -> None:
    """The reported rate must sit near the configured one, or the D2 cross-check is
    noise instead of a check.

    Before this was fixed the simulator emitted an unrelated ~0.9 L/h while a medium
    truck is configured at 2.4 L/h, so every segment reported roughly 60% divergence
    and the cross-check said nothing useful.
    """
    idling = _idling_samples_for_a_combustion_vehicle(seed=31)
    rates = [float(e["fuel_rate_lph"]) for e in idling]
    mean_rate = sum(rates) / len(rates)

    from fleet_common.domain.fuel import IDLE_BURN_RATES_LPH

    configured = set(IDLE_BURN_RATES_LPH.values())
    closest = min(configured, key=lambda r: abs(r - mean_rate))
    divergence = abs(mean_rate - closest) / closest
    assert divergence < 0.25, (
        f"mean reported idle rate {mean_rate:.2f} L/h is {divergence:.0%} from the "
        f"nearest configured rate {closest} L/h"
    )


def test_engine_off_samples_report_no_consumption() -> None:
    vehicle = _vehicle_with(Powertrain.DIESEL, seed=24)
    events, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(24), config=CLEAN)
    off = [e for e in events if e["ignition"] == Ignition.OFF.value]
    assert off, "no engine-off samples generated"
    assert all(e["fuel_rate_lph"] == 0.0 for e in off)
    assert all(e["rpm"] == 0 for e in off)


# --------------------------------------------------------------------------
# Fault injection (PDF S7)
# --------------------------------------------------------------------------


def test_duplicates_are_injected_and_are_exact_copies() -> None:
    """At-least-once delivery means duplicates happen, so the simulator produces
    them deliberately to prove dedup works."""
    config = JourneyConfig(
        duplicate_rate=1.0, out_of_order_rate=0.0, malformed_rate=0.0, dropout_rate=0.0
    )
    vehicle = build_fleet(vehicle_count=1, seed=30)[0]
    events, summary = generate_day(vehicle, DAY_START, rng=np.random.default_rng(30), config=config)
    assert summary.duplicates_injected > 0
    event_ids = [e["event_id"] for e in events]
    assert len(event_ids) > len(set(event_ids)), "no duplicate event ids present"


def test_out_of_order_events_are_injected() -> None:
    config = JourneyConfig(
        out_of_order_rate=1.0, duplicate_rate=0.0, malformed_rate=0.0, dropout_rate=0.0
    )
    vehicle = build_fleet(vehicle_count=1, seed=31)[0]
    events, summary = generate_day(vehicle, DAY_START, rng=np.random.default_rng(31), config=config)
    assert summary.out_of_order_injected > 0
    timestamps = [e["ts"] for e in events]
    assert timestamps != sorted(timestamps), "emission order is still event-time order"


def test_dropouts_create_the_gaps_that_need_bridging() -> None:
    """Gap tolerance in ADR-0001 exists for these; without them it is untested."""
    config = JourneyConfig(
        dropout_rate=0.5, duplicate_rate=0.0, out_of_order_rate=0.0, malformed_rate=0.0
    )
    vehicle = build_fleet(vehicle_count=1, seed=32)[0]
    _, summary = generate_day(vehicle, DAY_START, rng=np.random.default_rng(32), config=config)
    assert summary.samples_dropped > 0


def test_dtc_codes_are_injected_when_enabled() -> None:
    config = JourneyConfig(
        dtc_rate=1.0,
        duplicate_rate=0.0,
        out_of_order_rate=0.0,
        malformed_rate=0.0,
        dropout_rate=0.0,
    )
    vehicle = build_fleet(vehicle_count=1, seed=33)[0]
    events, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(33), config=config)
    assert all(e["dtc"] for e in events)


def test_no_faults_are_injected_when_all_rates_are_zero() -> None:
    vehicle = build_fleet(vehicle_count=1, seed=34)[0]
    _, summary = generate_day(vehicle, DAY_START, rng=np.random.default_rng(34), config=CLEAN)
    assert summary.duplicates_injected == 0
    assert summary.out_of_order_injected == 0
    assert summary.malformed_injected == 0
    assert summary.samples_dropped == 0


# --------------------------------------------------------------------------
# Duty cycle drives the utilisation spread (D3)
# --------------------------------------------------------------------------


def test_heavier_duty_cycles_produce_more_trips() -> None:
    """Without a spread in activity, every vehicle scores the same and the
    utilisation view has nothing to show."""
    fleet = build_fleet(vehicle_count=3_000, seed=40)
    rng = np.random.default_rng(40)

    trips_by_duty: dict[DutyCycle, list[int]] = {duty: [] for duty in DutyCycle}
    for vehicle in fleet[:600]:
        _, summary = generate_day(vehicle, DAY_START, rng=rng, config=CLEAN)
        trips_by_duty[vehicle.duty_cycle].append(summary.trips)

    heavy = trips_by_duty[DutyCycle.HEAVY]
    idle_asset = trips_by_duty[DutyCycle.IDLE_ASSET]
    assert heavy and idle_asset, "duty cycles under-represented in the sample"
    assert sum(heavy) / len(heavy) > sum(idle_asset) / len(idle_asset) * 3


def test_summary_accounts_for_every_emitted_event() -> None:
    vehicle = build_fleet(vehicle_count=1, seed=41)[0]
    events, summary = generate_day(vehicle, DAY_START, rng=np.random.default_rng(41), config=CLEAN)
    assert summary.event_count == len(events)
    assert summary.vin == vehicle.vin


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["duplicate_rate", "out_of_order_rate", "malformed_rate", "dtc_rate", "dropout_rate"],
)
@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_rates_must_be_probabilities(field: str, bad: float) -> None:
    with pytest.raises(ValueError, match="must be a probability"):
        JourneyConfig(**{field: bad})


@pytest.mark.parametrize("bad", [0, -10])
def test_sample_interval_must_be_positive(bad: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        JourneyConfig(sample_seconds=bad)


# --------------------------------------------------------------------------
# Property-based invariants
# --------------------------------------------------------------------------


@given(st.integers(min_value=0, max_value=5_000))
@settings(max_examples=25, deadline=None)
def test_property_generated_events_always_satisfy_the_contract(seed: int) -> None:
    """Whatever the seed, clean generation must never emit an invalid event."""
    for event in generate_for(seed=seed, vehicles=1, config=CLEAN):
        TelemetryEvent.model_validate(event)


@given(st.integers(min_value=0, max_value=5_000))
@settings(max_examples=25, deadline=None)
def test_property_detected_idle_never_exceeds_the_observed_span(seed: int) -> None:
    """The conservative-by-construction guarantee, end to end from the simulator."""
    vehicle = build_fleet(vehicle_count=1, seed=seed)[0]
    events, _ = generate_day(vehicle, DAY_START, rng=np.random.default_rng(seed), config=CLEAN)
    parsed = [TelemetryEvent.model_validate(e) for e in events]
    if len(parsed) < 2:
        return
    span = (parsed[-1].ts - parsed[0].ts).total_seconds()
    total_idle = sum(s.duration_seconds for s in segment_idle(parsed, IDLE_CONFIG))
    assert total_idle <= span + 1e-6
