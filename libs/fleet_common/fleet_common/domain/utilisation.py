"""Vehicle utilisation scoring (D3, ADR-0003).

Answers "which vehicles show low or abnormal utilisation?" - one of the five
questions the product must answer (CLAUDE.md S1).

A naive ``moving_time / engine_on_time`` ratio was rejected: a van that runs one
hour and moves 55 minutes of it scores 92% while sitting unused six days a week.
That inverts the answer the user needs. The composite below combines *how often*
a vehicle works, *how long* it works, and *how far* it travels, each relative to
its own vehicle class, so an under-used asset surfaces regardless of how
efficiently it behaves on the rare days it moves.

Outlier detection uses median and MAD rather than mean and standard deviation.
Mean and stdev are dragged by the very outliers we are hunting, so a single
extreme vehicle raises the bar enough to hide itself.

Pure domain code (CLAUDE.md S7). Complexity: O(1) per score;
O(n log n) for a fleet baseline, dominated by the median sort.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: Stamped onto every score so a figure stays attributable to its rules.
UTILISATION_FORMULA_VERSION: Final[str] = "utilisation-v1"

# Component weights (PROPOSED, D3). Config-driven in the services; these are the
# documented defaults. They sum to 1.0 - enforced at construction.
DEFAULT_WEIGHT_ACTIVE_DAY: Final[float] = 0.40
DEFAULT_WEIGHT_MOVING_HOURS: Final[float] = 0.35
DEFAULT_WEIGHT_DISTANCE: Final[float] = 0.25

DEFAULT_LOW_THRESHOLD: Final[float] = 40.0
DEFAULT_NORMAL_UPPER: Final[float] = 85.0
DEFAULT_ABNORMAL_Z: Final[float] = 3.0
DEFAULT_MAD_FLOOR_FRACTION: Final[float] = 0.10
DEFAULT_MIN_BASELINE_SAMPLE: Final[int] = 5

#: Scales MAD to be comparable with a standard deviation for normal data, so a
#: threshold of 3 means roughly "3 sigma" and stays intuitive.
_MAD_TO_SIGMA: Final[float] = 0.6745


class UtilisationBand(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    #: A statistical outlier on some component, e.g. high distance with almost no
    #: active days. Usually a data-quality or misuse signal rather than genuinely
    #: low utilisation, so it is reported separately and not lumped into LOW.
    ABNORMAL = "abnormal"


@dataclass(frozen=True, slots=True)
class UtilisationConfig:
    """Tunable utilisation parameters (D3)."""

    weight_active_day: float = DEFAULT_WEIGHT_ACTIVE_DAY
    weight_moving_hours: float = DEFAULT_WEIGHT_MOVING_HOURS
    weight_distance: float = DEFAULT_WEIGHT_DISTANCE
    low_threshold: float = DEFAULT_LOW_THRESHOLD
    normal_upper: float = DEFAULT_NORMAL_UPPER
    abnormal_z: float = DEFAULT_ABNORMAL_Z
    # Floors the MAD at a fraction of the class median. On a tightly clustered
    # class the raw MAD is tiny, so |z| > 3 fires on ordinary variation and the
    # ABNORMAL band fills with false positives. The floor refuses to claim more
    # precision than a small, tight sample can support.
    mad_floor_fraction: float = DEFAULT_MAD_FLOOR_FRACTION
    # Below this many peers, MAD is noise rather than spread, so abnormality is
    # not asserted at all - the vehicle is banded on its score alone.
    min_baseline_sample: int = DEFAULT_MIN_BASELINE_SAMPLE

    def __post_init__(self) -> None:
        total = self.weight_active_day + self.weight_moving_hours + self.weight_distance
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"weights must sum to 1.0, got {total}")
        if not 0.0 < self.low_threshold < self.normal_upper < 100.0:
            raise ValueError("require 0 < low_threshold < normal_upper < 100")
        if self.abnormal_z <= 0:
            raise ValueError("abnormal_z must be positive")
        if not 0.0 <= self.mad_floor_fraction < 1.0:
            raise ValueError("mad_floor_fraction must be in [0.0, 1.0)")
        if self.min_baseline_sample < 1:
            raise ValueError("min_baseline_sample must be at least 1")


@dataclass(frozen=True, slots=True)
class VehicleActivity:
    """Observed activity for one vehicle over one window.

    Inputs are raw counts and totals, not ratios: the formula owns the
    normalisation so it stays in one place and stays versioned.
    """

    vin: str
    window_days: int
    #: Days on which the vehicle moved at all.
    active_days: int
    moving_hours: float
    distance_km: float
    idle_hours: float

    def __post_init__(self) -> None:
        if self.window_days <= 0:
            raise ValueError("window_days must be positive")
        if self.active_days > self.window_days:
            raise ValueError("active_days cannot exceed window_days")
        if min(self.moving_hours, self.distance_km, self.idle_hours) < 0:
            raise ValueError("activity totals must be non-negative")

    @property
    def active_day_ratio(self) -> float:
        return self.active_days / self.window_days

    @property
    def moving_hours_per_day(self) -> float:
        return self.moving_hours / self.window_days

    @property
    def distance_km_per_day(self) -> float:
        return self.distance_km / self.window_days

    @property
    def idle_share(self) -> float:
        """Idle as a fraction of engine-on time; 0.0 when the engine never ran."""
        engine_on = self.moving_hours + self.idle_hours
        return self.idle_hours / engine_on if engine_on > 0 else 0.0


@dataclass(frozen=True, slots=True)
class ClassBaseline:
    """Robust per-class reference values, used to normalise each component.

    Built from peers in the same vehicle class so a heavy truck is not judged
    against a city car.
    """

    vehicle_class: str
    median_moving_hours_per_day: float
    median_distance_km_per_day: float
    mad_moving_hours_per_day: float
    mad_distance_km_per_day: float
    mad_active_day_ratio: float
    median_active_day_ratio: float
    sample_size: int


@dataclass(frozen=True, slots=True)
class UtilisationScore:
    """A utilisation score with its three components exposed.

    The components are returned alongside the score on purpose: a single opaque
    number will not survive a fleet manager asking "why is this 31?".
    """

    vin: str
    score: float
    band: UtilisationBand
    # --- components, each already normalised to 0.0-1.0 ------------------
    active_day_component: float
    moving_hours_component: float
    distance_component: float
    # --- context ---------------------------------------------------------
    idle_share: float
    formula_version: str
    window_days: int
    #: Largest robust z-score across components; drives the ABNORMAL band.
    max_abs_z: float
    abnormal_reason: str | None = None


def median(values: list[float]) -> float:
    """Median of a non-empty list."""
    if not values:
        raise ValueError("median of empty sequence")
    return float(statistics.median(values))


def median_absolute_deviation(values: list[float]) -> float:
    """MAD: the median of absolute deviations from the median.

    Returns 0.0 when every value is identical, which callers must treat as
    "no spread" rather than dividing by it.
    """
    if not values:
        raise ValueError("MAD of empty sequence")
    med = median(values)
    return float(statistics.median([abs(v - med) for v in values]))


def effective_mad(mad: float, med: float, floor_fraction: float) -> float:
    """Raise a very small MAD to a fraction of the median.

    A class whose members cluster tightly produces a near-zero MAD, which makes
    the robust z-score hypersensitive: a difference a fleet manager would call
    unremarkable reads as several sigma. Flooring the scale keeps the outlier
    test meaningful without inventing precision the sample cannot support.
    """
    return max(mad, floor_fraction * abs(med))


def robust_z(value: float, med: float, mad: float) -> float:
    """Modified z-score using median and MAD.

    When MAD is 0 the sample has no spread, so any deviation is unquantifiable
    rather than infinite; we report 0.0 for an exact match and a large sentinel
    otherwise, which keeps downstream comparisons finite and safe.
    """
    if mad <= 0.0:
        return 0.0 if value == med else float(DEFAULT_ABNORMAL_Z * 2)
    return _MAD_TO_SIGMA * (value - med) / mad


def build_class_baseline(vehicle_class: str, activities: list[VehicleActivity]) -> ClassBaseline:
    """Compute robust reference values for one vehicle class.

    Complexity: O(n log n) in the number of vehicles, from the median sorts.
    """
    if not activities:
        raise ValueError(f"cannot build a baseline for {vehicle_class!r} with no vehicles")
    moving = [a.moving_hours_per_day for a in activities]
    distance = [a.distance_km_per_day for a in activities]
    active = [a.active_day_ratio for a in activities]
    return ClassBaseline(
        vehicle_class=vehicle_class,
        median_moving_hours_per_day=median(moving),
        median_distance_km_per_day=median(distance),
        mad_moving_hours_per_day=median_absolute_deviation(moving),
        mad_distance_km_per_day=median_absolute_deviation(distance),
        median_active_day_ratio=median(active),
        mad_active_day_ratio=median_absolute_deviation(active),
        sample_size=len(activities),
    )


def _ratio_to_baseline(value: float, baseline_median: float) -> float:
    """Normalise a value against its class median, clamped to 0.0-1.0.

    Clamping at 1.0 means "at or above the class norm". Without the clamp, one
    very busy vehicle could score above 100 and distort every leaderboard.
    """
    if baseline_median <= 0.0:
        return 1.0 if value > 0.0 else 0.0
    return min(value / baseline_median, 1.0)


def score_utilisation(
    activity: VehicleActivity,
    baseline: ClassBaseline,
    config: UtilisationConfig | None = None,
) -> UtilisationScore:
    """Score one vehicle against its class baseline.

    Complexity: O(1).
    """
    cfg = config or UtilisationConfig()

    active_component = min(activity.active_day_ratio, 1.0)
    moving_component = _ratio_to_baseline(
        activity.moving_hours_per_day, baseline.median_moving_hours_per_day
    )
    distance_component = _ratio_to_baseline(
        activity.distance_km_per_day, baseline.median_distance_km_per_day
    )

    score = 100.0 * (
        cfg.weight_active_day * active_component
        + cfg.weight_moving_hours * moving_component
        + cfg.weight_distance * distance_component
    )
    score = max(0.0, min(score, 100.0))

    z_scores = {
        "moving_hours_per_day": robust_z(
            activity.moving_hours_per_day,
            baseline.median_moving_hours_per_day,
            effective_mad(
                baseline.mad_moving_hours_per_day,
                baseline.median_moving_hours_per_day,
                cfg.mad_floor_fraction,
            ),
        ),
        "distance_km_per_day": robust_z(
            activity.distance_km_per_day,
            baseline.median_distance_km_per_day,
            effective_mad(
                baseline.mad_distance_km_per_day,
                baseline.median_distance_km_per_day,
                cfg.mad_floor_fraction,
            ),
        ),
        "active_day_ratio": robust_z(
            activity.active_day_ratio,
            baseline.median_active_day_ratio,
            effective_mad(
                baseline.mad_active_day_ratio,
                baseline.median_active_day_ratio,
                cfg.mad_floor_fraction,
            ),
        ),
    }
    worst_component, worst_z = max(z_scores.items(), key=lambda kv: abs(kv[1]))
    max_abs_z = abs(worst_z)

    # With too few peers, MAD describes noise rather than spread, so we decline
    # to call anything abnormal and band on the score alone.
    baseline_is_usable = baseline.sample_size >= cfg.min_baseline_sample

    if baseline_is_usable and max_abs_z > cfg.abnormal_z:
        band = UtilisationBand.ABNORMAL
        reason = f"{worst_component} is {worst_z:+.1f} robust sigma from the class median"
    elif score < cfg.low_threshold:
        band = UtilisationBand.LOW
        reason = None
    elif score <= cfg.normal_upper:
        band = UtilisationBand.NORMAL
        reason = None
    else:
        band = UtilisationBand.HIGH
        reason = None

    return UtilisationScore(
        vin=activity.vin,
        score=score,
        band=band,
        active_day_component=active_component,
        moving_hours_component=moving_component,
        distance_component=distance_component,
        idle_share=activity.idle_share,
        formula_version=UTILISATION_FORMULA_VERSION,
        window_days=activity.window_days,
        max_abs_z=max_abs_z,
        abnormal_reason=reason,
    )
