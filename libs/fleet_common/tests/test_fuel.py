"""Tests for idle fuel and cost estimation (D2, D9, D11)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from fleet_common.domain.fuel import (
    FUEL_FORMULA_VERSION,
    BevHasNoIdleFuelError,
    FuelPrice,
    VehicleClass,
    estimate_idle_cost,
    estimate_idle_litres,
    idle_burn_rate_lph,
    measured_idle_litres,
    sum_idle_costs,
    to_money,
)
from fleet_common.events import Powertrain

PRICE = FuelPrice(price_id="inr-diesel-2026-09", price_per_litre=Decimal("92.50"), currency="INR")


# --------------------------------------------------------------------------
# The primary formula, and its auditability
# --------------------------------------------------------------------------


def test_one_hour_of_idle_burns_the_configured_rate() -> None:
    assert estimate_idle_litres(1.0, 2.4) == pytest.approx(2.4)


def test_cost_is_reproducible_by_hand() -> None:
    """A finance user must be able to check the arithmetic: 2.4 L/h x 1h x 92.50."""
    estimate = estimate_idle_cost(
        duration_seconds=3600,
        powertrain=Powertrain.DIESEL,
        vehicle_class=VehicleClass.MEDIUM_TRUCK,
        price=PRICE,
        idle_threshold_seconds=180,
    )
    assert estimate.litres_estimated == pytest.approx(2.4)
    assert estimate.cost == Decimal("222.0000")


def test_estimate_carries_its_full_assumption_set() -> None:
    """CLAUDE.md S14: every displayed figure must show what produced it."""
    estimate = estimate_idle_cost(
        duration_seconds=900,
        powertrain=Powertrain.DIESEL,
        vehicle_class=VehicleClass.HEAVY_TRUCK,
        price=PRICE,
        idle_threshold_seconds=180,
    )
    assert estimate.formula_version == FUEL_FORMULA_VERSION
    assert estimate.burn_rate_lph == 3.8
    assert estimate.fuel_price_per_litre == Decimal("92.50")
    assert estimate.price_id == "inr-diesel-2026-09"
    assert estimate.idle_threshold_seconds == 180
    assert estimate.currency == "INR"
    assert estimate.duration_seconds == 900


def test_zero_duration_costs_nothing() -> None:
    estimate = estimate_idle_cost(
        duration_seconds=0,
        powertrain=Powertrain.PETROL,
        vehicle_class=VehicleClass.CAR,
        price=PRICE,
        idle_threshold_seconds=180,
    )
    assert estimate.cost == Decimal("0.0000")


def test_negative_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        estimate_idle_cost(
            duration_seconds=-1,
            powertrain=Powertrain.PETROL,
            vehicle_class=VehicleClass.CAR,
            price=PRICE,
            idle_threshold_seconds=180,
        )


# --------------------------------------------------------------------------
# Money handling (D11)
# --------------------------------------------------------------------------


def test_money_uses_half_up_rounding() -> None:
    """Half-up, not banker's rounding: it matches what a user gets by hand."""
    assert to_money(Decimal("1.00005")) == Decimal("1.0001")
    assert to_money(Decimal("1.00015")) == Decimal("1.0002")


def test_money_is_quantised_to_four_places() -> None:
    """Matches numeric(14,4) in Postgres so nothing rounds at the boundary."""
    assert to_money(1 / 3).as_tuple().exponent == -4


def test_repeated_addition_does_not_drift() -> None:
    """The reason money is Decimal: float would accumulate error over a fleet."""
    tenth = to_money(Decimal("0.1"))
    assert sum([tenth] * 10, Decimal(0)) == Decimal("1.0000")


def test_price_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        FuelPrice(price_id="x", price_per_litre=Decimal("0"), currency="INR")


def test_currency_must_be_iso_4217_length() -> None:
    with pytest.raises(ValueError, match="ISO-4217"):
        FuelPrice(price_id="x", price_per_litre=Decimal("1"), currency="RUPEE")


# --------------------------------------------------------------------------
# BEV handling (D9)
# --------------------------------------------------------------------------


def test_bev_idle_fuel_raises_rather_than_returning_zero() -> None:
    """Zero would flow into a fleet total as if it were measured (D9)."""
    with pytest.raises(BevHasNoIdleFuelError):
        idle_burn_rate_lph(Powertrain.BEV, VehicleClass.CAR)


def test_bev_cost_estimate_raises() -> None:
    with pytest.raises(BevHasNoIdleFuelError):
        estimate_idle_cost(
            duration_seconds=600,
            powertrain=Powertrain.BEV,
            vehicle_class=VehicleClass.CAR,
            price=PRICE,
            idle_threshold_seconds=180,
        )


def test_powertrain_knows_whether_it_burns_fuel() -> None:
    assert Powertrain.DIESEL.burns_fuel
    assert Powertrain.HYBRID.burns_fuel
    assert not Powertrain.BEV.burns_fuel


def test_unconfigured_class_fails_loudly() -> None:
    """Failing beats inventing a default that would quietly produce money."""
    with pytest.raises(KeyError):
        idle_burn_rate_lph(Powertrain.HYBRID, VehicleClass.HEAVY_TRUCK)


# --------------------------------------------------------------------------
# Measured cross-check (D2)
# --------------------------------------------------------------------------


def test_cross_check_absent_when_the_device_reports_no_rate() -> None:
    estimate = estimate_idle_cost(
        duration_seconds=3600,
        powertrain=Powertrain.DIESEL,
        vehicle_class=VehicleClass.MEDIUM_TRUCK,
        price=PRICE,
        idle_threshold_seconds=180,
    )
    assert not estimate.has_cross_check
    assert estimate.measured_divergence_pct is None


def test_cross_check_reports_divergence_from_the_configured_rate() -> None:
    """An obviously wrong configured rate must become visible, not silent."""
    estimate = estimate_idle_cost(
        duration_seconds=3600,
        powertrain=Powertrain.DIESEL,
        vehicle_class=VehicleClass.MEDIUM_TRUCK,  # configured 2.4 L/h
        price=PRICE,
        idle_threshold_seconds=180,
        fuel_rate_samples=(3.0, 3.0, 3.0),  # device says 3.0 L/h
    )
    assert estimate.has_cross_check
    assert estimate.litres_measured == pytest.approx(3.0)
    assert estimate.measured_divergence_pct == pytest.approx(25.0)


def test_measured_litres_is_none_without_samples() -> None:
    assert measured_idle_litres((), 1.0) is None


# --------------------------------------------------------------------------
# Fleet aggregation (D9, D11)
# --------------------------------------------------------------------------


def _estimate(seconds: float) -> object:
    return estimate_idle_cost(
        duration_seconds=seconds,
        powertrain=Powertrain.DIESEL,
        vehicle_class=VehicleClass.MEDIUM_TRUCK,
        price=PRICE,
        idle_threshold_seconds=180,
    )


def test_fleet_total_sums_costs() -> None:
    total = sum_idle_costs([_estimate(3600), _estimate(3600)])  # type: ignore[list-item]
    assert total.total_cost == Decimal("444.0000")
    assert total.vehicle_count == 2


def test_fleet_total_flags_bev_exclusions() -> None:
    """The UI must be able to say 'excludes N BEVs' (D9)."""
    total = sum_idle_costs([_estimate(3600)], excluded_bev_count=3)  # type: ignore[list-item]
    assert total.excluded_bev_count == 3
    assert total.is_partial is True


def test_fleet_total_of_nothing_is_zero_and_complete() -> None:
    total = sum_idle_costs([])
    assert total.total_cost == Decimal("0.0000")
    assert total.is_partial is False


def test_mixing_currencies_is_an_error() -> None:
    """A cross-currency sum looks like money but means nothing."""
    usd = FuelPrice(price_id="usd", price_per_litre=Decimal("1.10"), currency="USD")
    other = estimate_idle_cost(
        duration_seconds=3600,
        powertrain=Powertrain.DIESEL,
        vehicle_class=VehicleClass.MEDIUM_TRUCK,
        price=usd,
        idle_threshold_seconds=180,
    )
    with pytest.raises(ValueError, match="cannot sum across currencies"):
        sum_idle_costs([_estimate(3600), other])  # type: ignore[list-item]


def test_mixing_formula_versions_is_an_error() -> None:
    """Totals must be attributable to one set of rules."""
    v2 = estimate_idle_cost(
        duration_seconds=3600,
        powertrain=Powertrain.DIESEL,
        vehicle_class=VehicleClass.MEDIUM_TRUCK,
        price=PRICE,
        idle_threshold_seconds=180,
        formula_version="idle-fuel-v2",
    )
    with pytest.raises(ValueError, match="cannot sum across formula versions"):
        sum_idle_costs([_estimate(3600), v2])  # type: ignore[list-item]
