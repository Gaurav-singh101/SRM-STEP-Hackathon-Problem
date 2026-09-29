"""Per-vehicle journey simulation: trips, stops and idle periods.

Produces the telemetry a real working day would produce, including the faults
PDF S7 asks for: bursty, out-of-order and duplicate events, plus malformed
payloads and sensor noise.

Why this emits plain dicts rather than ``TelemetryEvent`` objects: constructing and
validating a Pydantic model costs single-digit microseconds, which is fine for the
processor but becomes the ceiling for a producer targeting 100K events/s. The hot
path therefore builds dicts and serialises them with ``orjson``. The contract is
not weakened by this - ``test_journey_contract.py`` asserts that the emitted bytes
both validate against ``schemas/telemetry.raw.v1.json`` and parse into
``TelemetryEvent``, so a drift between the fast path and the contract fails CI.

This is the vectorised-producer mitigation named in ADR-0005.

Pure: no I/O, no clock, no unseeded randomness. Time and randomness are injected,
so a run is reproducible (CLAUDE.md S9).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import numpy as np

from fleet_common.domain.fuel import BevHasNoIdleFuelError, idle_burn_rate_lph
from fleet_common.events import SCHEMA_VERSION, Ignition
from simulator.fleet import DutyCycle, SyntheticVehicle

#: Sampling interval. Real telematics reports every 1-30 s depending on tariff;
#: 10 s keeps the walking skeleton's volume manageable while still producing
#: enough samples inside a 180 s idle threshold to be detected reliably.
DEFAULT_SAMPLE_SECONDS: Final[int] = 10

#: Trips per working day, by duty cycle. Drives the utilisation spread (D3).
TRIPS_PER_DAY: Final[dict[DutyCycle, tuple[int, int]]] = {
    DutyCycle.HEAVY: (8, 14),
    DutyCycle.NORMAL: (4, 8),
    DutyCycle.LIGHT: (1, 4),
    # Barely used: often zero trips in a day, which is what makes it show up as
    # an under-used asset rather than merely a quiet one.
    DutyCycle.IDLE_ASSET: (0, 1),
}

#: Namespace for deterministic event ids. uuid4() would make a rerun produce
#: different ids, so a failure could not be reproduced.
EVENT_ID_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f1e0000-0000-0000-0000-000000000001")

#: DTC codes the simulator can inject. P0505 is the idle-control family, which the
#: UI surfaces beside idle events; the others are ordinary faults.
FAULT_CODES: Final[tuple[str, ...]] = ("P0301", "P0420", "P0505", "P0128", "U0100")


@dataclass(frozen=True, slots=True)
class JourneyConfig:
    """Generation parameters. All injected; none hard-coded in the generator."""

    sample_seconds: int = DEFAULT_SAMPLE_SECONDS
    # Fault injection rates (PDF S7). Each is exercised by the processor's DLQ,
    # dedup and late-event paths, so they are features, not blemishes.
    duplicate_rate: float = 0.01
    out_of_order_rate: float = 0.02
    malformed_rate: float = 0.005
    # Chance a sample carries a DTC list.
    dtc_rate: float = 0.002
    # Chance a GPS fix is flagged invalid, so gap tolerance gets exercised.
    gps_invalid_rate: float = 0.01
    # Chance a sample is simply missing, producing the gaps that make bridging
    # necessary (D1).
    dropout_rate: float = 0.015

    def __post_init__(self) -> None:
        if self.sample_seconds <= 0:
            raise ValueError("sample_seconds must be positive")
        for name in (
            "duplicate_rate",
            "out_of_order_rate",
            "malformed_rate",
            "dtc_rate",
            "gps_invalid_rate",
            "dropout_rate",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be a probability, got {value}")


@dataclass(slots=True)
class _VehicleState:
    """Mutable per-vehicle cursor carried across a generated day."""

    odometer_km: float
    engine_hours: float
    fuel_level_pct: float
    #: The vehicle's configured idle burn rate, or None for a BEV. The device-reported
    #: fuel_rate_lph is generated around this value so the D2 measured cross-check is
    #: meaningful. An unrelated rate made the cross-check report roughly 60% divergence
    #: on every segment - noise rather than a signal, and it would invite the obvious
    #: question of why the system disagrees with itself.
    idle_burn_rate_lph: float | None = None
    seq: int = 0
    lat: float = 0.0
    lon: float = 0.0
    dropped: int = 0
    duplicated: int = 0
    reordered: int = 0
    malformed: int = 0
    idle_seconds_emitted: float = 0.0
    segments: list[tuple[str, float]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DaySummary:
    """What a generated day actually contained.

    Returned so tests and the demo can assert on ground truth. Without it there is
    no way to check that the processor found the idle the simulator produced, which
    is the end-to-end correctness question that matters most.
    """

    vin: str
    event_count: int
    trips: int
    idle_periods: int
    billable_idle_periods: int
    excessive_idle_periods: int
    total_idle_seconds: float
    moving_seconds: float
    distance_km: float
    duplicates_injected: int
    out_of_order_injected: int
    malformed_injected: int
    samples_dropped: int


def _configured_idle_rate(vehicle: SyntheticVehicle) -> float | None:
    """The vehicle's configured idle burn rate, or None for a BEV.

    Read from the cost engine's own table rather than duplicated, so the telemetry the
    simulator emits and the estimate the processor computes rest on the same number.
    Returns None for a pair with no configured rate: the roster should never produce
    one, but generating telemetry is not the place to fail if it somehow does.
    """
    if not vehicle.powertrain.burns_fuel:
        return None
    try:
        return idle_burn_rate_lph(vehicle.powertrain, vehicle.vehicle_class)
    except (BevHasNoIdleFuelError, KeyError):
        return None


def _idle_plan(
    rng: np.random.Generator, vehicle: SyntheticVehicle, config: JourneyConfig
) -> tuple[int, bool]:
    """Decide one stop's idle duration in seconds, and whether it is engine-on.

    ``idle_propensity`` is log-normal across the fleet, so a few vehicles idle far
    more than the rest. A uniform fleet would make the cost leaderboard flat and
    the product pointless.
    """
    engine_on = bool(rng.random() < min(0.35 * vehicle.idle_propensity, 0.9))
    if not engine_on:
        # Engine off: a genuine parked stop. Burns no fuel, so it is not idle waste.
        return int(rng.integers(120, 1800)), False

    # Deliberate spread across the three D1 bands, so all of them are exercised:
    # below threshold (not billed), billable, and excessive (alert).
    band = rng.random()
    if band < 0.45:
        seconds = int(rng.integers(20, 180))  # under threshold
    elif band < 0.85:
        seconds = int(rng.integers(180, 600))  # billable
    else:
        seconds = int(rng.integers(600, 2400))  # excessive -> alert
    return max(seconds, config.sample_seconds), True


def new_vehicle_state(vehicle: SyntheticVehicle, rng: np.random.Generator) -> _VehicleState:
    """Initial per-vehicle cursor: odometer, engine hours, fuel level and sequence.

    Created once per vehicle and then carried across days. A fresh state per day would
    restart ``seq`` at zero, which violates the monotonic-per-VIN contract in ADR-0004,
    and would let the odometer jump backwards between days.
    """
    return _VehicleState(
        odometer_km=float(rng.integers(5_000, 250_000)),
        engine_hours=float(rng.integers(200, 12_000)),
        fuel_level_pct=float(rng.integers(20, 95)),
        lat=vehicle.depot_lat,
        lon=vehicle.depot_lon,
        idle_burn_rate_lph=_configured_idle_rate(vehicle),
    )


def generate_day(
    vehicle: SyntheticVehicle,
    day_start: datetime,
    *,
    rng: np.random.Generator,
    config: JourneyConfig | None = None,
    state: _VehicleState | None = None,
) -> tuple[list[dict[str, Any]], DaySummary]:
    """Generate one working day of telemetry for one vehicle.

    Returns the events in emission order (which is deliberately *not* always event time
    order) together with a ground-truth summary.

    ``state`` carries the vehicle's cursor across days. Passing it is what keeps ``seq``
    monotonic per VIN and the odometer non-decreasing; omitting it starts a fresh
    vehicle, which is what a single-day caller wants.

    Complexity: O(k) in the number of samples produced.
    """
    cfg = config or JourneyConfig()
    state = state if state is not None else new_vehicle_state(vehicle, rng)

    low, high = TRIPS_PER_DAY[vehicle.duty_cycle]
    trips = int(rng.integers(low, high + 1))

    events: list[dict[str, Any]] = []
    # Shift start jitters, so the fleet does not move in lockstep and the burst at
    # shift start looks like a real burst.
    cursor = day_start + timedelta(minutes=int(rng.integers(0, 90)))

    idle_periods = billable = excessive = 0
    total_idle = moving_seconds = distance = 0.0

    for _ in range(trips):
        # --- drive segment -------------------------------------------------
        drive_seconds = int(rng.integers(6, 55)) * 60
        cruise_kph = float(rng.integers(25, 85))
        cursor, driven_km = _emit_drive(
            events, vehicle, state, cursor, drive_seconds, cruise_kph, cfg, rng
        )
        moving_seconds += drive_seconds
        distance += driven_km

        # --- stop ----------------------------------------------------------
        stop_seconds, engine_on = _idle_plan(rng, vehicle, cfg)
        cursor = _emit_stop(events, vehicle, state, cursor, stop_seconds, engine_on, cfg, rng)
        if engine_on:
            idle_periods += 1
            total_idle += stop_seconds
            if stop_seconds >= 180:
                billable += 1
            if stop_seconds >= 600:
                excessive += 1

    return events, DaySummary(
        vin=vehicle.vin,
        event_count=len(events),
        trips=trips,
        idle_periods=idle_periods,
        billable_idle_periods=billable,
        excessive_idle_periods=excessive,
        total_idle_seconds=total_idle,
        moving_seconds=moving_seconds,
        distance_km=distance,
        duplicates_injected=state.duplicated,
        out_of_order_injected=state.reordered,
        malformed_injected=state.malformed,
        samples_dropped=state.dropped,
    )


def generate_history(
    vehicle: SyntheticVehicle,
    first_day_start: datetime,
    *,
    days: int,
    rng: np.random.Generator,
    config: JourneyConfig | None = None,
) -> tuple[list[dict[str, Any]], list[DaySummary]]:
    """Generate consecutive days for one vehicle, carrying its state across them.

    The reason this exists rather than leaving callers to loop: generating each day
    independently restarts ``seq`` at zero, and the dedup layer then correctly treats
    every day after the first as replayed data and discards it. That failure is silent
    from the producer's side - the events are accepted by the broker, and only the
    consumer's idle-event count reveals it.
    """
    state = new_vehicle_state(vehicle, rng)
    events: list[dict[str, Any]] = []
    summaries: list[DaySummary] = []

    for offset in range(days):
        day_events, summary = generate_day(
            vehicle,
            first_day_start + timedelta(days=offset),
            rng=rng,
            config=config,
            state=state,
        )
        events.extend(day_events)
        summaries.append(summary)

    return events, summaries


def _emit_drive(
    events: list[dict[str, Any]],
    vehicle: SyntheticVehicle,
    state: _VehicleState,
    cursor: datetime,
    duration_seconds: int,
    cruise_kph: float,
    cfg: JourneyConfig,
    rng: np.random.Generator,
) -> tuple[datetime, float]:
    """Emit samples for one moving segment. Returns the new cursor and distance."""
    steps = max(duration_seconds // cfg.sample_seconds, 1)
    distance = 0.0

    for _ in range(steps):
        # Speed wanders around the cruise value; never negative.
        speed = max(0.0, cruise_kph + float(rng.normal(0.0, 6.0)))
        leg_km = speed * cfg.sample_seconds / 3600.0
        distance += leg_km
        state.odometer_km += leg_km
        state.engine_hours += cfg.sample_seconds / 3600.0
        state.fuel_level_pct = max(3.0, state.fuel_level_pct - leg_km * 0.03)
        # Drift the position so consecutive fixes are plausible.
        state.lat += float(rng.normal(0.0, 0.0006))
        state.lon += float(rng.normal(0.0, 0.0006))

        _append(events, vehicle, state, cursor, speed, Ignition.ON, cfg, rng)
        cursor += timedelta(seconds=cfg.sample_seconds)

    return cursor, distance


def _emit_stop(
    events: list[dict[str, Any]],
    vehicle: SyntheticVehicle,
    state: _VehicleState,
    cursor: datetime,
    duration_seconds: int,
    engine_on: bool,
    cfg: JourneyConfig,
    rng: np.random.Generator,
) -> datetime:
    """Emit samples for one stationary period."""
    steps = max(duration_seconds // cfg.sample_seconds, 1)
    ignition = Ignition.ON if engine_on else Ignition.OFF

    for _ in range(steps):
        # Idling still burns fuel, which is the entire point of the product.
        if engine_on:
            state.engine_hours += cfg.sample_seconds / 3600.0
            state.fuel_level_pct = max(3.0, state.fuel_level_pct - 0.004)
            state.idle_seconds_emitted += cfg.sample_seconds

        # Stationary GPS jitter: a real receiver wanders by a few metres at rest.
        # This is what makes hysteresis necessary (ADR-0001) - without the jitter
        # the state machine would never be tested against the case it exists for.
        speed = abs(float(rng.normal(0.0, 0.6))) if engine_on else 0.0

        _append(events, vehicle, state, cursor, speed, ignition, cfg, rng)
        cursor += timedelta(seconds=cfg.sample_seconds)

    return cursor


#: Sensor noise on the reported consumption rate, as a fraction of the true value.
#: Real flow meters are not exact, and a perfectly clean signal would make the D2
#: cross-check a tautology rather than a check.
FUEL_RATE_NOISE_SIGMA: Final[float] = 0.08


def _fuel_rate(
    state: _VehicleState, speed_kph: float, engine_on: bool, rng: np.random.Generator
) -> float:
    """Device-reported instantaneous consumption, in litres/hour.

    Built around the vehicle's *configured* idle burn rate so the measured cross-check
    compares like with like: a few percent of divergence normally, and a large
    divergence only when the configured rate is genuinely wrong.

    Under load the rate rises with speed, which is why a moving sample reports far more
    than an idling one.
    """
    if not engine_on:
        return 0.0
    base = state.idle_burn_rate_lph
    if base is None:
        return 0.0
    load_factor = 1.0 + speed_kph * 0.055
    noisy = base * load_factor * (1.0 + float(rng.normal(0.0, FUEL_RATE_NOISE_SIGMA)))
    return round(max(0.0, noisy), 2)


def _append(
    events: list[dict[str, Any]],
    vehicle: SyntheticVehicle,
    state: _VehicleState,
    ts: datetime,
    speed_kph: float,
    ignition: Ignition,
    cfg: JourneyConfig,
    rng: np.random.Generator,
) -> None:
    """Build one event dict and apply fault injection."""
    # Dropout: the sample never existed. Produces the gaps that make bridging
    # necessary, and beyond tolerance, segment splitting correct.
    if rng.random() < cfg.dropout_rate:
        state.dropped += 1
        return

    state.seq += 1
    engine_on = ignition is Ignition.ON

    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(
            uuid.uuid5(EVENT_ID_NAMESPACE, f"{vehicle.vin}:{state.seq}:{ts.isoformat()}")
        ),
        "tenant_id": vehicle.tenant_id,
        "device_id": f"dev-{vehicle.vin[-6:]}",
        "vin": vehicle.vin,
        "seq": state.seq,
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "speed_kph": round(speed_kph, 2),
        "odometer_km": round(state.odometer_km, 2),
        "heading_deg": round(float(rng.random()) * 359.9, 1),
        "ignition": ignition.value,
        "engine_state": (("idling" if speed_kph < 2.0 else "running") if engine_on else "off"),
        "rpm": int(700 + speed_kph * 22) if engine_on else 0,
        "engine_hours": round(state.engine_hours, 2),
        "lat": round(state.lat, 6),
        "lon": round(state.lon, 6),
        "gps_valid": bool(rng.random() >= cfg.gps_invalid_rate),
        "hdop": round(0.6 + float(rng.random()) * 1.8, 2),
        "pto_active": False,
    }

    # BEVs report state of charge, not a fuel level. Emitting both would contradict
    # the vehicle master and let a BEV acquire a fuel cost (D9).
    if vehicle.powertrain.burns_fuel:
        event["fuel_level_pct"] = round(state.fuel_level_pct, 1)
        event["fuel_rate_lph"] = _fuel_rate(state, speed_kph, engine_on, rng)
        event["soc_pct"] = None
    else:
        event["fuel_level_pct"] = None
        event["fuel_rate_lph"] = None
        event["soc_pct"] = round(max(5.0, state.fuel_level_pct), 1)

    if rng.random() < cfg.dtc_rate:
        event["dtc"] = [FAULT_CODES[int(rng.integers(0, len(FAULT_CODES)))]]
    else:
        event["dtc"] = None
    event["evt"] = None

    # Malformed: an impossible speed. Must reach the DLQ with a reason code and
    # must never crash the consumer (CLAUDE.md S8).
    if rng.random() < cfg.malformed_rate:
        event["speed_kph"] = 999.0
        state.malformed += 1

    events.append(event)

    # Duplicate: at-least-once delivery means the processor sees these anyway, so
    # the simulator produces them on purpose to prove dedup works.
    if rng.random() < cfg.duplicate_rate:
        events.append(dict(event))
        state.duplicated += 1

    # Out of order: swap with the previous event so the consumer receives them
    # reversed, exercising the late/out-of-order path.
    if len(events) >= 2 and rng.random() < cfg.out_of_order_rate:
        events[-1], events[-2] = events[-2], events[-1]
        state.reordered += 1
