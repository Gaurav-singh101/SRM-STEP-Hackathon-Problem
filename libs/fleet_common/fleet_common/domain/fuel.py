"""Idle fuel and cost estimation (D2, ADR-0002).

Every figure this module returns carries the full assumption set that produced
it - formula version, burn rate, fuel price and the idle threshold in force.
That is a hard product requirement, not decoration (CLAUDE.md S14): this is a
finance tool, and a number a finance manager cannot reproduce by hand is a
number they will not act on.

Primary estimate is a configured burn rate per (powertrain, vehicle class).
Where the device reports ``fuel_rate_lph`` we also compute a *measured*
cross-check and the divergence between the two, so an obviously wrong
configured rate becomes visible instead of silently producing wrong money.

Money is ``Decimal`` throughout (D11). Binary floating point cannot represent
most decimal fractions exactly, so accumulating float currency across millions
of segments drifts - and a drifting cost total is worse than no total.

Pure domain code: no I/O, no clock (CLAUDE.md S7). Complexity O(1) per segment.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from fleet_common.events import Powertrain

#: Version stamped onto every estimate. Bump when the formula changes so old
#: figures stay attributable to the rules that produced them.
FUEL_FORMULA_VERSION: Final[str] = "idle-fuel-v1"

#: Money scale. Matches numeric(14,4) in Postgres (D11) so the database and the
#: domain agree on precision and no rounding happens at the boundary.
MONEY_SCALE: Final[Decimal] = Decimal("0.0001")


class VehicleClass(StrEnum):
    CAR = "car"
    LIGHT_VAN = "light_van"
    MEDIUM_TRUCK = "medium_truck"
    HEAVY_TRUCK = "heavy_truck"


#: Idle burn rates in litres/hour, keyed (powertrain, vehicle class).
#:
#: STATUS: PROPOSED synthetic values (D2). These are plausible engineering
#: placeholders for a synthetic fleet, NOT measured or sourced figures, and they
#: must not be presented as either. Hybrids are lower because the engine cycles
#: off at rest. BEVs are absent by design - see ``idle_burn_rate_lph``.
IDLE_BURN_RATES_LPH: Final[dict[tuple[Powertrain, VehicleClass], float]] = {
    (Powertrain.PETROL, VehicleClass.CAR): 0.9,
    (Powertrain.PETROL, VehicleClass.LIGHT_VAN): 1.1,
    (Powertrain.DIESEL, VehicleClass.CAR): 0.8,
    (Powertrain.DIESEL, VehicleClass.LIGHT_VAN): 1.2,
    (Powertrain.DIESEL, VehicleClass.MEDIUM_TRUCK): 2.4,
    (Powertrain.DIESEL, VehicleClass.HEAVY_TRUCK): 3.8,
    (Powertrain.HYBRID, VehicleClass.CAR): 0.4,
    (Powertrain.HYBRID, VehicleClass.LIGHT_VAN): 0.6,
}


class BevHasNoIdleFuelError(ValueError):
    """Raised when idle fuel is requested for a battery-electric vehicle.

    Explicit rather than returning zero: zero would silently flow into a fleet
    total as if it were a measured value, understating nothing but hiding the
    fact that the vehicle is simply out of scope for fuel cost (D9).
    """


def idle_burn_rate_lph(powertrain: Powertrain, vehicle_class: VehicleClass) -> float:
    """Configured idle burn rate in litres/hour.

    Raises:
        BevHasNoIdleFuelError: for BEVs, which burn no fuel at rest (D9).
        KeyError: for a (powertrain, class) pair with no configured rate. Failing
            loudly beats inventing a default that would quietly produce money.
    """
    if not powertrain.burns_fuel:
        raise BevHasNoIdleFuelError(
            f"{powertrain} has no idle fuel consumption; BEVs are tracked for utilisation only (D9)"
        )
    return IDLE_BURN_RATES_LPH[(powertrain, vehicle_class)]


@dataclass(frozen=True, slots=True)
class FuelPrice:
    """A fuel price as of a point in time.

    ``price_id`` is recorded on every estimate so recomputing a past period uses
    that period's price and any historical figure stays reproducible (D11).
    """

    price_id: str
    price_per_litre: Decimal
    currency: str

    def __post_init__(self) -> None:
        if self.price_per_litre <= 0:
            raise ValueError("price_per_litre must be positive")
        if len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter ISO-4217 code")


@dataclass(frozen=True, slots=True)
class IdleCostEstimate:
    """An idle fuel/cost estimate plus everything needed to reproduce it.

    Labelled an *estimate* deliberately and everywhere (CLAUDE.md S14).
    """

    litres_estimated: float
    cost: Decimal
    currency: str
    # --- assumptions, surfaced in the API and the UI --------------------
    formula_version: str
    burn_rate_lph: float
    fuel_price_per_litre: Decimal
    price_id: str
    idle_threshold_seconds: int
    duration_seconds: float
    # --- optional measured cross-check (D2) ----------------------------
    litres_measured: float | None = None
    #: Signed divergence of measured from estimated, as a percentage. Large
    #: values mean the configured burn rate disagrees with the device.
    measured_divergence_pct: float | None = None

    @property
    def has_cross_check(self) -> bool:
        return self.litres_measured is not None


def estimate_idle_litres(duration_hours: float, burn_rate_lph: float) -> float:
    """Litres burned while idling. The whole primary formula, deliberately trivial."""
    if duration_hours < 0:
        raise ValueError("duration_hours must be non-negative")
    if burn_rate_lph < 0:
        raise ValueError("burn_rate_lph must be non-negative")
    return duration_hours * burn_rate_lph


def measured_idle_litres(
    fuel_rate_samples: tuple[float, ...], duration_hours: float
) -> float | None:
    """Litres implied by the device's own ``fuel_rate_lph`` samples.

    Uses the mean sample rate over the segment. Returns ``None`` when the device
    reported no rate, which is common - the cross-check is opportunistic.
    """
    if not fuel_rate_samples or duration_hours < 0:
        return None
    mean_rate = sum(fuel_rate_samples) / len(fuel_rate_samples)
    return mean_rate * duration_hours


def to_money(value: Decimal | float) -> Decimal:
    """Quantise to the money scale with half-up rounding.

    Half-up (not banker's rounding) because that is what a finance user gets
    when they check the arithmetic by hand.
    """
    as_decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    return as_decimal.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def estimate_idle_cost(
    *,
    duration_seconds: float,
    powertrain: Powertrain,
    vehicle_class: VehicleClass,
    price: FuelPrice,
    idle_threshold_seconds: int,
    fuel_rate_samples: tuple[float, ...] = (),
    formula_version: str = FUEL_FORMULA_VERSION,
) -> IdleCostEstimate:
    """Estimate the fuel and money lost to one idle segment.

    Keyword-only: this function returns money, and a transposed positional
    argument here would be an expensive, silent mistake.

    Raises:
        BevHasNoIdleFuelError: for BEVs (D9). Callers aggregating a mixed fleet
            must catch this and report the exclusion, never coerce it to zero.
    """
    if duration_seconds < 0:
        raise ValueError("duration_seconds must be non-negative")

    burn_rate = idle_burn_rate_lph(powertrain, vehicle_class)
    duration_hours = duration_seconds / 3600.0
    litres = estimate_idle_litres(duration_hours, burn_rate)
    cost = to_money(Decimal(str(litres)) * price.price_per_litre)

    litres_measured = measured_idle_litres(fuel_rate_samples, duration_hours)
    divergence: float | None = None
    if litres_measured is not None and litres > 0:
        divergence = (litres_measured - litres) / litres * 100.0

    return IdleCostEstimate(
        litres_estimated=litres,
        cost=cost,
        currency=price.currency,
        formula_version=formula_version,
        burn_rate_lph=burn_rate,
        fuel_price_per_litre=price.price_per_litre,
        price_id=price.price_id,
        idle_threshold_seconds=idle_threshold_seconds,
        duration_seconds=duration_seconds,
        litres_measured=litres_measured,
        measured_divergence_pct=divergence,
    )


@dataclass(frozen=True, slots=True)
class FleetCostTotal:
    """Aggregated idle cost across a set of vehicles.

    ``excluded_bev_count`` exists so the UI can say "excludes N BEVs" next to
    the total. A fleet figure that silently omits vehicles is a wrong figure,
    and a finance user would under-read fleet cost without ever knowing (D9).
    """

    total_cost: Decimal
    total_litres: float
    currency: str
    vehicle_count: int
    excluded_bev_count: int
    formula_version: str

    @property
    def is_partial(self) -> bool:
        return self.excluded_bev_count > 0


def sum_idle_costs(
    estimates: list[IdleCostEstimate], *, excluded_bev_count: int = 0
) -> FleetCostTotal:
    """Total a list of estimates, refusing to mix currencies or formula versions.

    Mixing either would produce a meaningless number that still looks like money,
    so both are hard errors rather than warnings.
    """
    if not estimates:
        return FleetCostTotal(
            total_cost=to_money(Decimal(0)),
            total_litres=0.0,
            currency="",
            vehicle_count=0,
            excluded_bev_count=excluded_bev_count,
            formula_version=FUEL_FORMULA_VERSION,
        )

    currencies = {e.currency for e in estimates}
    if len(currencies) > 1:
        raise ValueError(f"cannot sum across currencies: {sorted(currencies)}")
    versions = {e.formula_version for e in estimates}
    if len(versions) > 1:
        raise ValueError(f"cannot sum across formula versions: {sorted(versions)}")

    return FleetCostTotal(
        total_cost=to_money(sum((e.cost for e in estimates), Decimal(0))),
        total_litres=sum(e.litres_estimated for e in estimates),
        currency=currencies.pop(),
        vehicle_count=len(estimates),
        excluded_bev_count=excluded_bev_count,
        formula_version=versions.pop(),
    )
