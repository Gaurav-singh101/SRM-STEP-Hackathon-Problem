"""Deterministic synthetic fleet roster.

Builds the vehicle population the simulator drives and the seed job writes to
Postgres. Both must agree exactly - a telemetry event for a VIN that is not in the
vehicles table has no powertrain, so it has no cost - so the roster is a pure
function of (seed, count) and is generated identically by both.

PDF S8 requires 100,000+ vehicles with realistic trips, faults and noise, and
names the simulator as part of the solution rather than a test fixture.

Complexity: O(n) in vehicle count, with VIN construction O(1) per vehicle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import numpy as np
import numpy.typing as npt

from fleet_common.domain.fuel import IDLE_BURN_RATES_LPH, VehicleClass
from fleet_common.events import Powertrain
from fleet_common.vin import repair_check_digit

#: VIN alphabet: 33 characters, excluding I, O and Q (confusable with 1 and 0).
VIN_ALPHABET: Final[str] = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
VIN_BASE: Final[int] = len(VIN_ALPHABET)

#: Synthetic World Manufacturer Identifiers. Deliberately not real WMIs: PDF S14
#: forbids real vehicle data, and a real WMI would imply a real manufacturer.
SYNTHETIC_WMIS: Final[tuple[str, ...]] = ("ZZA", "ZZB", "ZZC", "ZZD")

#: Serial field length, VIN positions 12-17.
SERIAL_LENGTH: Final[int] = 6

#: Fleet composition (PROPOSED, D9). ICE-dominant with a small EV tail, so the
#: BEV exclusion path is exercised by the demo rather than left untested.
#:
#: NOTE: this is the mix *drawn*, not the mix *realised*. Petrol and hybrid trucks
#: have no configured burn rate, so they are repaired to diesel below, moving about
#: 6 percentage points into diesel. Quantified by
#: services/simulator/tests/test_fleet.py::test_the_repair_moves_the_expected_share_into_diesel.
POWERTRAIN_MIX: Final[dict[Powertrain, float]] = {
    Powertrain.DIESEL: 0.80,
    Powertrain.PETROL: 0.12,
    Powertrain.HYBRID: 0.05,
    Powertrain.BEV: 0.03,
}

#: Class mix. Commercial-vehicle heavy, because idle cost is a commercial problem.
CLASS_MIX: Final[dict[VehicleClass, float]] = {
    VehicleClass.LIGHT_VAN: 0.45,
    VehicleClass.MEDIUM_TRUCK: 0.25,
    VehicleClass.CAR: 0.20,
    VehicleClass.HEAVY_TRUCK: 0.10,
}

#: Pairs with no configured burn rate, DERIVED from the cost engine's own table
#: rather than hand-listed. A hand-written list drifts: the first version named only
#: hybrid trucks and missed petrol trucks, so the roster emitted vehicles whose cost
#: could not be computed. Deriving it means the roster and the cost engine cannot
#: disagree (ADR-0002).
UNSUPPORTED_PAIRS: Final[frozenset[tuple[Powertrain, VehicleClass]]] = frozenset(
    (powertrain, vehicle_class)
    for powertrain in Powertrain
    if powertrain.burns_fuel
    for vehicle_class in VehicleClass
    if (powertrain, vehicle_class) not in IDLE_BURN_RATES_LPH
)


class DutyCycle(StrEnum):
    """How hard a vehicle is worked.

    Drives the utilisation spread. Without a deliberate mix, every vehicle scores
    the same and the utilisation view has nothing to show (D3).
    """

    HEAVY = "heavy"
    NORMAL = "normal"
    LIGHT = "light"
    #: Barely used: the asset a finance manager should question. These are the
    #: vehicles the utilisation leaderboard exists to surface.
    IDLE_ASSET = "idle_asset"


DUTY_MIX: Final[dict[DutyCycle, float]] = {
    DutyCycle.HEAVY: 0.20,
    DutyCycle.NORMAL: 0.55,
    DutyCycle.LIGHT: 0.18,
    DutyCycle.IDLE_ASSET: 0.07,
}


@dataclass(frozen=True, slots=True)
class SyntheticVehicle:
    """One vehicle in the synthetic fleet."""

    vin: str
    tenant_id: str
    fleet_index: int
    powertrain: Powertrain
    vehicle_class: VehicleClass
    fuel_type: str | None
    duty_cycle: DutyCycle
    model_year: int
    registration: str
    # Depot coordinates, so idle events cluster at real sites and geohash
    # grouping has something to group (PDF S9).
    depot_lat: float
    depot_lon: float
    #: Multiplier on this vehicle's tendency to idle. The spread is what makes an
    #: idle-cost leaderboard meaningful rather than uniform noise.
    idle_propensity: float


def encode_serial(index: int, length: int = SERIAL_LENGTH) -> str:
    """Encode ``index`` in the VIN alphabet, fixed width, most significant first.

    Guarantees VIN uniqueness by construction: distinct indices give distinct
    serials, so no collision check is needed even at a million vehicles.

    Raises:
        ValueError: if ``index`` does not fit in ``length`` characters.
    """
    if index < 0:
        raise ValueError("index must be non-negative")
    capacity = VIN_BASE**length
    if index >= capacity:
        raise ValueError(f"index {index} does not fit in {length} VIN characters")

    chars = []
    remaining = index
    for _ in range(length):
        chars.append(VIN_ALPHABET[remaining % VIN_BASE])
        remaining //= VIN_BASE
    return "".join(reversed(chars))


def build_vin(index: int) -> str:
    """Construct a structurally valid, unique synthetic VIN for ``index``.

    Layout follows ISO 3779 positions so the result looks realistic: WMI (1-3),
    descriptor (4-8), check digit (9), year (10), plant (11), serial (12-17). The
    check digit is computed last, so every generated VIN passes ``is_valid_vin``.

    Deliberately a function of ``index`` **only** - not of the drawn powertrain,
    class or model year. That makes a VIN a stable identity: vehicle 7 is the same
    VIN at every seed and every fleet size, so the demo can run at 2,000 vehicles
    and the benchmark at 100,000 while referring to the same vehicles. Encoding the
    drawn attributes would have been marginally more realistic while making the
    identity move whenever any unrelated parameter changed.

    Nothing reads meaning out of a VIN anyway: class and powertrain come from the
    vehicle master in Postgres, which ADR-0004 makes their single source of truth.
    """
    wmi = SYNTHETIC_WMIS[index % len(SYNTHETIC_WMIS)]
    descriptor = encode_serial(index // len(SYNTHETIC_WMIS), length=5)
    year_code = VIN_ALPHABET[index % VIN_BASE]
    plant = "A"
    serial = encode_serial(index)
    draft = f"{wmi}{descriptor}0{year_code}{plant}{serial}"
    if len(draft) != 17:
        raise AssertionError(f"constructed VIN has wrong length: {draft!r}")
    return repair_check_digit(draft)


def _weighted_choices(
    rng: np.random.Generator, weights: Sequence[float], size: int
) -> npt.NDArray[np.int64]:
    """Draw ``size`` indices into ``weights`` according to those weights."""
    probabilities = np.asarray(weights, dtype=np.float64)
    probabilities = probabilities / probabilities.sum()
    drawn: npt.NDArray[np.int64] = rng.choice(
        len(probabilities), size=size, p=probabilities
    ).astype(np.int64)
    return drawn


def build_fleet(
    *,
    vehicle_count: int,
    seed: int,
    tenant_ids: tuple[str, ...] = ("tenant-demo",),
    fleets_per_tenant: int = 4,
    depot_count: int = 12,
) -> list[SyntheticVehicle]:
    """Generate the synthetic fleet roster.

    Deterministic: the same (seed, vehicle_count) always yields the same roster,
    byte for byte. Without that, a measurement cannot be repeated and the seed job
    and the simulator could disagree about which vehicles exist (CLAUDE.md S9).

    Vectorised with numpy rather than looped: at 100K vehicles a per-vehicle Python
    loop over several random draws is the kind of avoidable cost that makes the
    simulator itself the bottleneck (CLAUDE.md S9).
    """
    if vehicle_count <= 0:
        raise ValueError("vehicle_count must be positive")
    if not tenant_ids:
        raise ValueError("at least one tenant is required")

    # One independent stream per attribute, spawned from the seed. With a single
    # shared generator each draw consumes a different amount of randomness for a
    # different vehicle_count, so every later attribute shifts and a 100-vehicle
    # roster is not the prefix of a 200-vehicle one. Independent streams keep the
    # roster stable under changes to fleet size.
    streams = np.random.SeedSequence(seed).spawn(6)
    powertrain_rng = np.random.default_rng(streams[0])
    class_rng = np.random.default_rng(streams[1])
    duty_rng = np.random.default_rng(streams[2])
    year_rng = np.random.default_rng(streams[3])
    propensity_rng = np.random.default_rng(streams[4])
    depot_rng = np.random.default_rng(streams[5])

    powertrain_options = list(POWERTRAIN_MIX)
    class_options = list(CLASS_MIX)
    duty_options = list(DUTY_MIX)

    powertrain_idx = _weighted_choices(powertrain_rng, list(POWERTRAIN_MIX.values()), vehicle_count)
    class_idx = _weighted_choices(class_rng, list(CLASS_MIX.values()), vehicle_count)
    duty_idx = _weighted_choices(duty_rng, list(DUTY_MIX.values()), vehicle_count)
    model_years = year_rng.integers(2016, 2027, size=vehicle_count)
    # Log-normal: most vehicles idle a normal amount and a few idle a great deal,
    # which is the shape a real fleet shows and what makes a top-K leaderboard
    # informative instead of flat.
    idle_propensity = propensity_rng.lognormal(mean=0.0, sigma=0.55, size=vehicle_count)

    # Depots clustered around a plausible metro area. Coordinates are synthetic.
    # Drawn from a fixed-size stream so depot positions do not move with fleet size.
    depot_lats = 12.90 + depot_rng.random(depot_count) * 0.25
    depot_lons = 77.50 + depot_rng.random(depot_count) * 0.30
    depot_assignment = depot_rng.integers(0, depot_count, size=vehicle_count)

    vehicles: list[SyntheticVehicle] = []
    for i in range(vehicle_count):
        powertrain = powertrain_options[int(powertrain_idx[i])]
        vehicle_class = class_options[int(class_idx[i])]

        # Repair combinations that ADR-0002 leaves without a burn rate, rather than
        # emitting a vehicle whose cost cannot be computed. Diesel is configured for
        # every class, so it is always a valid landing point; changing the class
        # instead would distort the class mix that utilisation baselines rest on.
        if (powertrain, vehicle_class) in UNSUPPORTED_PAIRS:
            powertrain = Powertrain.DIESEL

        fuel_type: str | None
        if powertrain is Powertrain.BEV:
            fuel_type = None
        elif powertrain is Powertrain.PETROL or powertrain is Powertrain.HYBRID:
            fuel_type = "petrol"
        else:
            fuel_type = "diesel"

        tenant_id = tenant_ids[i % len(tenant_ids)]
        model_year = int(model_years[i])
        depot = int(depot_assignment[i])

        vehicles.append(
            SyntheticVehicle(
                vin=build_vin(i),
                tenant_id=tenant_id,
                fleet_index=i % fleets_per_tenant,
                powertrain=powertrain,
                vehicle_class=vehicle_class,
                fuel_type=fuel_type,
                duty_cycle=duty_options[int(duty_idx[i])],
                model_year=model_year,
                registration=f"SYN-{i:07d}",
                depot_lat=float(depot_lats[depot]),
                depot_lon=float(depot_lons[depot]),
                idle_propensity=float(idle_propensity[i]),
            )
        )

    return vehicles
