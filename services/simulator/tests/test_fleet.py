"""Tests for the synthetic fleet roster.

The roster is a pure function of (seed, count), and that property carries real
weight: the seed job writes vehicles to Postgres while the simulator emits
telemetry for them, independently. If the two disagreed, events would arrive for
VINs with no powertrain, and a vehicle with no powertrain has no cost.
"""

from __future__ import annotations

from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fleet_common.domain.fuel import IDLE_BURN_RATES_LPH, idle_burn_rate_lph
from fleet_common.events import Powertrain
from fleet_common.vin import is_valid_vin
from simulator.fleet import (
    CLASS_MIX,
    DUTY_MIX,
    POWERTRAIN_MIX,
    UNSUPPORTED_PAIRS,
    VIN_ALPHABET,
    DutyCycle,
    build_fleet,
    build_vin,
    encode_serial,
)

# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_same_seed_produces_an_identical_roster() -> None:
    """Without this, no measurement can be repeated (CLAUDE.md S9)."""
    a = build_fleet(vehicle_count=500, seed=42)
    b = build_fleet(vehicle_count=500, seed=42)
    assert [v.vin for v in a] == [v.vin for v in b]
    assert [v.powertrain for v in a] == [v.powertrain for v in b]
    assert [v.duty_cycle for v in a] == [v.duty_cycle for v in b]
    assert [v.idle_propensity for v in a] == [v.idle_propensity for v in b]


def test_different_seeds_produce_different_attributes() -> None:
    """VINs are index-derived so they are stable across seeds; the drawn attributes
    are not. That split is deliberate: a VIN is an identity, not a random value."""
    a = build_fleet(vehicle_count=500, seed=1)
    b = build_fleet(vehicle_count=500, seed=2)
    assert [v.vin for v in a] == [v.vin for v in b]
    assert [v.powertrain for v in a] != [v.powertrain for v in b]
    assert [v.duty_cycle for v in a] != [v.duty_cycle for v in b]


def test_roster_is_a_prefix_as_count_grows() -> None:
    """A 100-vehicle roster is the first 100 of a 200-vehicle roster, attributes
    included. Lets the demo run small and the benchmark run large while referring to
    the same vehicles - which requires independent per-attribute random streams,
    since a single shared generator shifts every later draw when the count changes.
    """
    small = build_fleet(vehicle_count=100, seed=9)
    large = build_fleet(vehicle_count=200, seed=9)
    assert [v.vin for v in small] == [v.vin for v in large[:100]]
    assert [v.powertrain for v in small] == [v.powertrain for v in large[:100]]
    assert [v.duty_cycle for v in small] == [v.duty_cycle for v in large[:100]]
    assert [v.depot_lat for v in small] == [v.depot_lat for v in large[:100]]


# --------------------------------------------------------------------------
# VIN construction
# --------------------------------------------------------------------------


def test_every_generated_vin_is_valid() -> None:
    """Generated VINs must pass the same validator the ingest path applies, or the
    simulator would fill the DLQ with its own output."""
    for vehicle in build_fleet(vehicle_count=2_000, seed=3):
        assert is_valid_vin(vehicle.vin), vehicle.vin


def test_vins_are_unique_by_construction() -> None:
    fleet = build_fleet(vehicle_count=5_000, seed=4)
    assert len({v.vin for v in fleet}) == len(fleet)


def test_serial_encoding_is_injective() -> None:
    """Distinct indices must give distinct serials; this is what makes VIN
    uniqueness structural rather than a collision check."""
    serials = {encode_serial(i) for i in range(20_000)}
    assert len(serials) == 20_000


def test_serial_is_fixed_width() -> None:
    assert len(encode_serial(0)) == len(encode_serial(999_999)) == 6


def test_serial_uses_only_the_vin_alphabet() -> None:
    for index in (0, 1, 32, 33, 1089, 999_999):
        assert set(encode_serial(index)) <= set(VIN_ALPHABET)


def test_serial_rejects_an_index_that_does_not_fit() -> None:
    """Fail loudly rather than silently wrapping and colliding VINs."""
    capacity = len(VIN_ALPHABET) ** 2
    with pytest.raises(ValueError, match="does not fit"):
        encode_serial(capacity, length=2)


def test_serial_rejects_a_negative_index() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        encode_serial(-1)


def test_generated_vins_avoid_confusable_letters() -> None:
    for vehicle in build_fleet(vehicle_count=1_000, seed=5):
        assert not (set(vehicle.vin) & {"I", "O", "Q"})


def test_vin_is_seventeen_characters() -> None:
    assert len(build_vin(0)) == 17


def test_no_real_world_wmi_is_used() -> None:
    """PDF S14 forbids real vehicle data; a real WMI would imply a real maker."""
    real_prefixes = {"1HG", "5GZ", "WP0", "WVW", "JHM", "WBA", "1FT"}
    for vehicle in build_fleet(vehicle_count=200, seed=6):
        assert vehicle.vin[:3] not in real_prefixes


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


def test_every_vehicle_has_a_configured_burn_rate_or_is_a_bev() -> None:
    """The decisive consistency check between roster and cost engine.

    ADR-0002 deliberately leaves hybrid trucks unconfigured and the cost engine
    raises on an unconfigured pair. So the roster must never emit one, or a
    legitimately existing vehicle would have an uncomputable cost.
    """
    for vehicle in build_fleet(vehicle_count=20_000, seed=8):
        if not vehicle.powertrain.burns_fuel:
            continue
        # Must not raise.
        assert idle_burn_rate_lph(vehicle.powertrain, vehicle.vehicle_class) > 0


def test_no_vehicle_uses_an_unsupported_pair() -> None:
    for vehicle in build_fleet(vehicle_count=20_000, seed=8):
        assert (vehicle.powertrain, vehicle.vehicle_class) not in UNSUPPORTED_PAIRS


def test_unsupported_pairs_really_are_unconfigured() -> None:
    """Guards the premise of the test above: if a burn rate were later added for a
    hybrid truck, UNSUPPORTED_PAIRS would be stale and the repair unnecessary."""
    for pair in UNSUPPORTED_PAIRS:
        assert pair not in IDLE_BURN_RATES_LPH


def test_bevs_have_no_fuel_type() -> None:
    """A BEV with a fuel type would violate the schema constraint and could pick up
    a fuel cost (D9)."""
    for vehicle in build_fleet(vehicle_count=5_000, seed=10):
        if vehicle.powertrain is Powertrain.BEV:
            assert vehicle.fuel_type is None
        else:
            assert vehicle.fuel_type in {"diesel", "petrol"}


def test_fleet_contains_some_bevs() -> None:
    """The BEV exclusion path must be exercised by the demo, not left untested."""
    fleet = build_fleet(vehicle_count=2_000, seed=11)
    bevs = [v for v in fleet if v.powertrain is Powertrain.BEV]
    assert bevs, "no BEVs generated: the D9 exclusion path would go undemonstrated"


def test_fleet_contains_under_used_vehicles() -> None:
    """The utilisation view needs low-utilisation vehicles to show (D3)."""
    fleet = build_fleet(vehicle_count=2_000, seed=12)
    idle_assets = [v for v in fleet if v.duty_cycle is DutyCycle.IDLE_ASSET]
    assert idle_assets, "no under-used vehicles: the utilisation view has nothing to show"


def expected_powertrain_mix() -> dict[Powertrain, float]:
    """The mix after the unsupported-pair repair, derived from the declared inputs.

    POWERTRAIN_MIX is the mix *drawn*, not the mix *realised*: a petrol or hybrid
    truck has no configured burn rate, so it is repaired to diesel. That moves
    roughly 6 percentage points into diesel. Computing the expectation rather than
    widening a tolerance keeps the test honest and self-updating - change a weight
    and it still holds.
    """
    realised = dict(POWERTRAIN_MIX)
    for powertrain, vehicle_class in UNSUPPORTED_PAIRS:
        moved = POWERTRAIN_MIX[powertrain] * CLASS_MIX[vehicle_class]
        realised[powertrain] -= moved
        realised[Powertrain.DIESEL] += moved
    return realised


def test_composition_matches_the_mix_after_repair() -> None:
    """Sampling tolerance only: the expectation itself is computed, not guessed."""
    fleet = build_fleet(vehicle_count=20_000, seed=13)
    counts = Counter(v.powertrain for v in fleet)
    expected = expected_powertrain_mix()

    for powertrain, share in expected.items():
        observed = counts[powertrain] / len(fleet)
        assert abs(observed - share) < 0.015, (
            f"{powertrain}: observed {observed:.3f}, expected {share:.3f}"
        )


def test_the_repair_moves_the_expected_share_into_diesel() -> None:
    """Quantifies the distortion so it is a known consequence, not a hidden one."""
    expected = expected_powertrain_mix()
    drift = expected[Powertrain.DIESEL] - POWERTRAIN_MIX[Powertrain.DIESEL]
    assert 0.05 < drift < 0.07, f"repair drift is {drift:.3f}, expected about 0.06"


def test_repaired_powertrains_lose_exactly_what_diesel_gains() -> None:
    """The mix must still sum to 1.0: the repair reassigns, it does not create."""
    assert sum(expected_powertrain_mix().values()) == pytest.approx(1.0)


def test_idle_propensity_is_spread_not_uniform() -> None:
    """A uniform fleet makes the idle-cost leaderboard flat and the product
    pointless, so the spread is a requirement, not an artefact."""
    fleet = build_fleet(vehicle_count=5_000, seed=14)
    propensities = sorted(v.idle_propensity for v in fleet)
    p10 = propensities[len(propensities) // 10]
    p90 = propensities[len(propensities) * 9 // 10]
    assert p90 / p10 > 2.5, "idle propensity is too uniform to produce a leaderboard"


def test_all_propensities_are_positive() -> None:
    for vehicle in build_fleet(vehicle_count=1_000, seed=15):
        assert vehicle.idle_propensity > 0


def test_mix_weights_sum_to_one() -> None:
    for name, mix in (
        ("POWERTRAIN_MIX", POWERTRAIN_MIX),
        ("CLASS_MIX", CLASS_MIX),
        ("DUTY_MIX", DUTY_MIX),
    ):
        assert sum(mix.values()) == pytest.approx(1.0), name


def test_vehicles_are_spread_across_tenants() -> None:
    fleet = build_fleet(vehicle_count=100, seed=16, tenant_ids=("tenant-a", "tenant-b", "tenant-c"))
    assert len({v.tenant_id for v in fleet}) == 3


def test_vehicles_are_spread_across_fleets_and_depots() -> None:
    fleet = build_fleet(vehicle_count=500, seed=17, fleets_per_tenant=4)
    assert len({v.fleet_index for v in fleet}) == 4
    # Depots must cluster, so geohash grouping has real groups (PDF S9).
    assert 1 < len({(v.depot_lat, v.depot_lon) for v in fleet}) <= 12


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, -1])
def test_non_positive_vehicle_count_is_refused(count: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        build_fleet(vehicle_count=count, seed=1)


def test_empty_tenant_list_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one tenant"):
        build_fleet(vehicle_count=10, seed=1, tenant_ids=())


# --------------------------------------------------------------------------
# Property-based invariants
# --------------------------------------------------------------------------


@given(st.integers(min_value=0, max_value=33**6 - 1))
@settings(max_examples=300)
def test_property_any_index_encodes_to_a_valid_serial(index: int) -> None:
    serial = encode_serial(index)
    assert len(serial) == 6
    assert set(serial) <= set(VIN_ALPHABET)


@given(st.integers(min_value=0, max_value=50_000))
@settings(max_examples=300)
def test_property_any_index_builds_a_valid_vin(index: int) -> None:
    assert is_valid_vin(build_vin(index))


@given(st.integers(min_value=1, max_value=300), st.integers(min_value=0, max_value=10_000))
@settings(max_examples=60, deadline=None)
def test_property_roster_is_always_internally_consistent(count: int, seed: int) -> None:
    for vehicle in build_fleet(vehicle_count=count, seed=seed):
        assert is_valid_vin(vehicle.vin)
        assert (vehicle.powertrain.burns_fuel) == (vehicle.fuel_type is not None)
        assert (vehicle.powertrain, vehicle.vehicle_class) not in UNSUPPORTED_PAIRS
