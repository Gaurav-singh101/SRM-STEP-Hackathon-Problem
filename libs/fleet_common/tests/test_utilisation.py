"""Tests for utilisation scoring (D3).

The first test is the one that justifies the whole formula choice: it encodes the
failure mode of the naive ratio we rejected.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fleet_common.domain.utilisation import (
    UTILISATION_FORMULA_VERSION,
    ClassBaseline,
    UtilisationBand,
    UtilisationConfig,
    VehicleActivity,
    build_class_baseline,
    effective_mad,
    median_absolute_deviation,
    robust_z,
    score_utilisation,
)


def busy_fleet(n: int = 9) -> list[VehicleActivity]:
    """A reference fleet of vehicles working most days, with realistic spread.

    The spread is deliberate and deterministic. A perfectly uniform fleet has a
    MAD of zero, which correctly makes *any* deviation abnormal - true, but it
    leaves no room to test the ordinary bands. Real fleets vary, so the baseline
    fixture does too.
    """
    return [
        VehicleActivity(
            vin=f"VIN{i:03d}",
            window_days=30,
            active_days=20 + (i % 5),
            moving_hours=120.0 + (i % 5) * 6.0,
            distance_km=4000.0 + (i % 5) * 200.0,
            idle_hours=18.0 + (i % 3) * 2.0,
        )
        for i in range(n)
    ]


BASELINE = build_class_baseline("light_van", busy_fleet())


# --------------------------------------------------------------------------
# The case that rejected the naive moving/engine-on ratio
# --------------------------------------------------------------------------


def test_efficient_but_barely_used_vehicle_scores_low() -> None:
    """Moves 55 of every 60 engine-on minutes, but works only 2 days in 30.

    A moving/engine-on ratio would score this 92% and hide it. The composite has
    to surface it, because it is exactly the asset a finance manager should sell.
    """
    idle_but_efficient = VehicleActivity(
        vin="LAZY",
        window_days=30,
        active_days=2,
        moving_hours=1.83,
        distance_km=60.0,
        idle_hours=0.17,
    )
    assert idle_but_efficient.idle_share == pytest.approx(0.085, abs=0.01)

    score = score_utilisation(idle_but_efficient, BASELINE)
    assert score.score < 20.0
    assert score.band in (UtilisationBand.LOW, UtilisationBand.ABNORMAL)


#: The fleet member whose activity equals the class median on all three
#: components (active_days 22, moving 132 h, distance 4400 km).
MEDIAN_VEHICLE_INDEX = 2


def test_vehicle_matching_its_class_scores_high() -> None:
    """A vehicle exactly at its class median should land in the HIGH band.

    Its moving-hours and distance components clamp to 1.0 by definition of the
    baseline, so only the active-day ratio holds the score below 100.
    """
    score = score_utilisation(busy_fleet()[MEDIAN_VEHICLE_INDEX], BASELINE)
    assert score.score > 85.0
    assert score.band is UtilisationBand.HIGH


def test_components_are_exposed_alongside_the_score() -> None:
    """A single opaque number will not survive 'why is this 31?'."""
    score = score_utilisation(busy_fleet()[MEDIAN_VEHICLE_INDEX], BASELINE)
    assert score.active_day_component == pytest.approx(22 / 30)
    assert score.moving_hours_component == pytest.approx(1.0)
    assert score.distance_component == pytest.approx(1.0)
    assert score.formula_version == UTILISATION_FORMULA_VERSION
    assert score.window_days == 30


#: A vehicle modestly below its class on every component - the kind of ordinary
#: variation a fleet manager would not remark on. Its raw robust z against this
#: tightly clustered baseline is -3.6, which is what motivated the MAD floor.
UNREMARKABLE_VEHICLE = VehicleActivity(
    vin="ORDINARY",
    window_days=30,
    active_days=18,
    moving_hours=100.0,
    distance_km=3400.0,
    idle_hours=12.0,
)


def test_mad_floor_prevents_a_false_outlier() -> None:
    """Ordinary variation inside a tight class must not read as an outlier.

    The class median is 132 h with a raw MAD of only 6 h, so 100 h computes to
    -3.6 robust sigma and the ABNORMAL band fills with false positives. Flooring
    the scale at 10% of the median brings it back to a sane -1.6.
    """
    score = score_utilisation(UNREMARKABLE_VEHICLE, BASELINE)
    assert score.band is UtilisationBand.NORMAL
    assert score.max_abs_z == pytest.approx(1.635, abs=0.01)


def test_mad_floor_can_be_disabled() -> None:
    """Setting the fraction to 0 restores the raw, hypersensitive behaviour.

    Kept configurable so the trade-off stays visible and tunable rather than
    silently baked into the formula.
    """
    raw = UtilisationConfig(mad_floor_fraction=0.0)
    score = score_utilisation(UNREMARKABLE_VEHICLE, BASELINE, raw)
    assert score.max_abs_z == pytest.approx(3.597, abs=0.01)
    assert score.band is UtilisationBand.ABNORMAL


def test_small_baseline_never_asserts_abnormality() -> None:
    """MAD from 3 peers is noise, so we decline to call anything abnormal."""
    tiny = build_class_baseline("tiny", busy_fleet(3))
    weird = VehicleActivity(
        vin="WEIRD",
        window_days=30,
        active_days=1,
        moving_hours=400.0,
        distance_km=50000.0,
        idle_hours=1.0,
    )
    score = score_utilisation(weird, tiny)
    assert tiny.sample_size == 3
    assert score.band is not UtilisationBand.ABNORMAL


def test_score_is_bounded_to_0_100() -> None:
    superstar = VehicleActivity(
        vin="BUSY",
        window_days=30,
        active_days=30,
        moving_hours=600.0,
        distance_km=40000.0,
        idle_hours=1.0,
    )
    assert score_utilisation(superstar, BASELINE).score == pytest.approx(100.0)


def test_completely_unused_vehicle_scores_zero() -> None:
    parked = VehicleActivity(
        vin="PARKED",
        window_days=30,
        active_days=0,
        moving_hours=0.0,
        distance_km=0.0,
        idle_hours=0.0,
    )
    score = score_utilisation(parked, BASELINE)
    assert score.score == pytest.approx(0.0)
    assert score.idle_share == 0.0


# --------------------------------------------------------------------------
# Band classification
# --------------------------------------------------------------------------


def test_abnormal_beats_low_and_explains_itself() -> None:
    """High distance with almost no active days is a data-quality signal, not
    low utilisation, so it must be reported separately."""
    weird = VehicleActivity(
        vin="WEIRD",
        window_days=30,
        active_days=1,
        moving_hours=200.0,
        distance_km=30000.0,
        idle_hours=1.0,
    )
    score = score_utilisation(weird, BASELINE)
    assert score.band is UtilisationBand.ABNORMAL
    assert score.abnormal_reason is not None
    assert "robust sigma" in score.abnormal_reason


def test_ordinary_bands_carry_no_abnormal_reason() -> None:
    """Only the ABNORMAL band explains itself; the others need no caveat."""
    mid = VehicleActivity(
        vin="MID",
        window_days=30,
        active_days=18,
        moving_hours=100.0,
        distance_km=3400.0,
        idle_hours=12.0,
    )
    score = score_utilisation(mid, BASELINE)
    assert score.band is not UtilisationBand.ABNORMAL
    assert score.abnormal_reason is None


# --------------------------------------------------------------------------
# Robust statistics: the reason we do not use mean/stdev
# --------------------------------------------------------------------------


def test_mad_is_not_dragged_by_a_single_extreme_value() -> None:
    """With mean/stdev, one extreme vehicle raises the bar enough to hide itself."""
    clean = [10.0] * 9
    with_outlier = [*clean, 10_000.0]
    assert median_absolute_deviation(clean) == pytest.approx(0.0)
    assert median_absolute_deviation(with_outlier) == pytest.approx(0.0)


def test_effective_mad_floors_a_tiny_spread() -> None:
    assert effective_mad(0.5, 100.0, 0.10) == pytest.approx(10.0)


def test_effective_mad_leaves_a_healthy_spread_alone() -> None:
    assert effective_mad(25.0, 100.0, 0.10) == pytest.approx(25.0)


def test_robust_z_is_zero_when_there_is_no_spread_and_value_matches() -> None:
    assert robust_z(5.0, 5.0, 0.0) == 0.0


def test_robust_z_flags_deviation_when_there_is_no_spread() -> None:
    """A deviation from a zero-spread sample is unquantifiable, not ignorable."""
    assert abs(robust_z(9.0, 5.0, 0.0)) > 3.0


def test_baseline_requires_at_least_one_vehicle() -> None:
    with pytest.raises(ValueError, match="no vehicles"):
        build_class_baseline("empty", [])


def test_baseline_reports_its_sample_size() -> None:
    """A baseline built from 2 peers is far weaker than one built from 200."""
    assert build_class_baseline("light_van", busy_fleet(4)).sample_size == 4


# --------------------------------------------------------------------------
# Input and config validation
# --------------------------------------------------------------------------


def test_active_days_cannot_exceed_the_window() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        VehicleActivity(
            vin="X",
            window_days=7,
            active_days=8,
            moving_hours=1.0,
            distance_km=1.0,
            idle_hours=0.0,
        )


def test_window_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        VehicleActivity(
            vin="X",
            window_days=0,
            active_days=0,
            moving_hours=0.0,
            distance_km=0.0,
            idle_hours=0.0,
        )


def test_negative_totals_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        VehicleActivity(
            vin="X",
            window_days=7,
            active_days=1,
            moving_hours=-1.0,
            distance_km=0.0,
            idle_hours=0.0,
        )


def test_weights_must_sum_to_one() -> None:
    """Otherwise the score silently stops being a percentage."""
    with pytest.raises(ValueError, match=r"must sum to 1\.0"):
        UtilisationConfig(weight_active_day=0.5, weight_moving_hours=0.5, weight_distance=0.5)


def test_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match=r"low_threshold < normal_upper"):
        UtilisationConfig(low_threshold=90.0, normal_upper=40.0)


def test_zero_baseline_median_does_not_divide_by_zero() -> None:
    """A whole class that never moves must not crash the scorer."""
    idle_baseline = ClassBaseline(
        vehicle_class="static",
        median_moving_hours_per_day=0.0,
        median_distance_km_per_day=0.0,
        mad_moving_hours_per_day=0.0,
        mad_distance_km_per_day=0.0,
        mad_active_day_ratio=0.0,
        median_active_day_ratio=0.0,
        sample_size=3,
    )
    activity = VehicleActivity(
        vin="X",
        window_days=30,
        active_days=5,
        moving_hours=10.0,
        distance_km=100.0,
        idle_hours=2.0,
    )
    score = score_utilisation(activity, idle_baseline)
    assert 0.0 <= score.score <= 100.0


# --------------------------------------------------------------------------
# Property-based invariants
# --------------------------------------------------------------------------


@st.composite
def _activities(draw: st.DrawFn) -> VehicleActivity:
    """Generate a valid VehicleActivity.

    active_days is drawn against the window it was generated for, so the
    invariant holds by construction. A .filter() cannot work here: the
    constructor validates and raises before Hypothesis gets to reject.
    """
    window_days = draw(st.integers(min_value=1, max_value=90))
    return VehicleActivity(
        vin="PROP",
        window_days=window_days,
        active_days=draw(st.integers(min_value=0, max_value=window_days)),
        moving_hours=draw(st.floats(min_value=0.0, max_value=2000.0, allow_nan=False)),
        distance_km=draw(st.floats(min_value=0.0, max_value=100_000.0, allow_nan=False)),
        idle_hours=draw(st.floats(min_value=0.0, max_value=2000.0, allow_nan=False)),
    )


_activity = _activities()


@given(_activity)
@settings(max_examples=300, deadline=None)
def test_property_score_is_always_a_percentage(activity: VehicleActivity) -> None:
    assert 0.0 <= score_utilisation(activity, BASELINE).score <= 100.0


@given(_activity)
@settings(max_examples=300, deadline=None)
def test_property_components_are_always_normalised(activity: VehicleActivity) -> None:
    score = score_utilisation(activity, BASELINE)
    for component in (
        score.active_day_component,
        score.moving_hours_component,
        score.distance_component,
    ):
        assert 0.0 <= component <= 1.0


@given(_activity)
@settings(max_examples=300, deadline=None)
def test_property_idle_share_is_a_fraction(activity: VehicleActivity) -> None:
    assert 0.0 <= activity.idle_share <= 1.0


@given(
    st.integers(min_value=0, max_value=30),
    st.integers(min_value=0, max_value=30),
)
@settings(max_examples=200, deadline=None)
def test_property_more_active_days_never_lowers_the_score(a: int, b: int) -> None:
    """Monotonicity: working more days must never make a vehicle look worse."""
    fewer, more = min(a, b), max(a, b)

    def build(days: int) -> VehicleActivity:
        return VehicleActivity(
            vin="M",
            window_days=30,
            active_days=days,
            moving_hours=60.0,
            distance_km=2000.0,
            idle_hours=10.0,
        )

    assert score_utilisation(build(fewer), BASELINE).score <= (
        score_utilisation(build(more), BASELINE).score + 1e-9
    )
